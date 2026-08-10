// The Cloud Control bridge: ConfigHub units carrying CFN-shaped resources,
// applied to AWS (or an emulator of it) through the Cloud Control API.
//
// ConfigHub's SDK ships no AWS toolchain — units are ConfigHub/YAML,
// Kubernetes/YAML or AppConfig/*. This bridge is what the confighub arm
// deploys with, and the writeup must say so: the score measures
// ConfigHub-the-store plus this bridge, not a vendor apply path.
//
// A unit looks like:
//
//	resources:
//	  - label: gateVpc
//	    typeName: AWS::EC2::VPC
//	    desiredState:
//	      CidrBlock: 10.42.0.0/16
//
// The bridge's live view mirrors it, with each resource's Cloud Control
// identifier and last-read live properties under `live`:
//
//	resources:
//	  - label: gateVpc
//	    typeName: AWS::EC2::VPC
//	    identifier: vpc-0123
//	    desiredState: {...}
//	    live: {...}
//
// One target per region; the region is the BridgeHandle. The AWS client comes
// from LoadDefaultConfig, so AWS_ENDPOINT_URL is the only wiring an emulator
// needs — the same story as the Formae arm, deliberately.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"time"

	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/cloudcontrol"
	cctypes "github.com/aws/aws-sdk-go-v2/service/cloudcontrol/types"
	funcapi "github.com/confighub/sdk/core/function/api"
	"github.com/confighub/sdk/core/worker/api"
	"github.com/confighub/sdk/core/workerapi"
	"gopkg.in/yaml.v3"
)

const ProviderCloudControl = api.ProviderType("AWSCloudControl")

// Unit is the YAML document a ConfigHub unit holds for this bridge.
type Unit struct {
	Resources []Resource `yaml:"resources"`
}

// Resource is one Cloud Control resource in a unit.
type Resource struct {
	Label        string         `yaml:"label"`
	TypeName     string         `yaml:"typeName"`
	Identifier   string         `yaml:"identifier,omitempty"`
	DesiredState map[string]any `yaml:"desiredState"`
	Live         map[string]any `yaml:"live,omitempty"`
}

// ccAPI is the slice of Cloud Control this bridge calls; a test fakes it.
type ccAPI interface {
	CreateResource(ctx context.Context, in *cloudcontrol.CreateResourceInput, opts ...func(*cloudcontrol.Options)) (*cloudcontrol.CreateResourceOutput, error)
	GetResource(ctx context.Context, in *cloudcontrol.GetResourceInput, opts ...func(*cloudcontrol.Options)) (*cloudcontrol.GetResourceOutput, error)
	DeleteResource(ctx context.Context, in *cloudcontrol.DeleteResourceInput, opts ...func(*cloudcontrol.Options)) (*cloudcontrol.DeleteResourceOutput, error)
	ListResources(ctx context.Context, in *cloudcontrol.ListResourcesInput, opts ...func(*cloudcontrol.Options)) (*cloudcontrol.ListResourcesOutput, error)
	GetResourceRequestStatus(ctx context.Context, in *cloudcontrol.GetResourceRequestStatusInput, opts ...func(*cloudcontrol.Options)) (*cloudcontrol.GetResourceRequestStatusOutput, error)
}

// CloudControlBridge implements the ConfigHub bridge interface over ccAPI.
type CloudControlBridge struct {
	regions   []string
	newClient func(region string) (ccAPI, error)
}

func NewCloudControlBridge(regions []string) *CloudControlBridge {
	return &CloudControlBridge{
		regions: regions,
		newClient: func(region string) (ccAPI, error) {
			cfg, err := awsconfig.LoadDefaultConfig(context.Background(), awsconfig.WithRegion(region))
			if err != nil {
				return nil, err
			}
			return cloudcontrol.NewFromConfig(cfg), nil
		},
	}
}

func (b *CloudControlBridge) ID() api.BridgeWorkerID {
	return api.BridgeWorkerID{
		ProviderType:   ProviderCloudControl,
		ToolchainTypes: []workerapi.ToolchainType{workerapi.ToolchainConfigHubYAML},
	}
}

func (b *CloudControlBridge) Info(opts api.InfoOptions) api.BridgeInfo {
	var targets []api.Target
	for _, region := range b.regions {
		targets = append(targets, api.Target{
			BridgeHandle: region,
			Name:         api.GenerateTargetName(opts.WorkerSlug, ProviderCloudControl, workerapi.ToolchainConfigHubYAML, region),
		})
	}
	return api.BridgeInfo{
		SupportedConfigTypes: []*api.SupportedConfigType{
			{
				ConfigTypeSignature: api.ConfigTypeSignature{
					ConfigType: api.ConfigType{
						ToolchainType: workerapi.ToolchainConfigHubYAML,
						ProviderType:  ProviderCloudControl,
					},
					Options: []api.BridgeOption{
						{
							Name:        "ImportTypes",
							Description: "Comma-separated Cloud Control type names Import sweeps (defaults to the unit's own types)",
							Required:    false,
							DataType:    funcapi.DataTypeString,
							Example:     "AWS::EC2::VPC,AWS::EC2::Subnet",
						},
					},
				},
				AvailableTargets: targets,
			},
		},
	}
}

func parseUnit(data []byte) (Unit, error) {
	var u Unit
	if err := yaml.Unmarshal(data, &u); err != nil {
		return u, fmt.Errorf("unit is not the bridge's YAML shape: %w", err)
	}
	for i, r := range u.Resources {
		if r.TypeName == "" {
			return u, fmt.Errorf("resources[%d] has no typeName", i)
		}
		if r.Label == "" {
			return u, fmt.Errorf("resources[%d] (%s) has no label", i, r.TypeName)
		}
	}
	return u, nil
}

func renderUnit(u Unit) ([]byte, error) {
	return yaml.Marshal(u)
}

// desiredJSON is the DesiredState as the JSON Cloud Control expects.
func desiredJSON(r Resource) (string, error) {
	buf, err := json.Marshal(r.DesiredState)
	if err != nil {
		return "", fmt.Errorf("%s: desiredState does not marshal: %w", r.Label, err)
	}
	return string(buf), nil
}

// driftedResources compares each resource's desired properties against its
// live read: every desired key must be present live with an equal value.
// Live-only keys (identifiers, defaults the service filled in) are not drift —
// the same rule Cloud Control's own drift detection applies.
func driftedResources(u Unit) []string {
	var drifted []string
	for _, r := range u.Resources {
		if r.Live == nil {
			drifted = append(drifted, r.Label)
			continue
		}
		for k, want := range r.DesiredState {
			got, ok := r.Live[k]
			if !ok || !looselyEqual(want, got) {
				drifted = append(drifted, r.Label)
				break
			}
		}
	}
	return drifted
}

// looselyEqual compares YAML-decoded desired values against JSON-decoded live
// ones, where 1 may arrive as int, int64 or float64 depending on the decoder.
func looselyEqual(a, b any) bool {
	if reflect.DeepEqual(a, b) {
		return true
	}
	ja, errA := json.Marshal(a)
	jb, errB := json.Marshal(b)
	return errA == nil && errB == nil && string(ja) == string(jb)
}

func (b *CloudControlBridge) client(payload api.BridgePayload) (ccAPI, string, error) {
	region := payload.BridgeHandle
	if region == "" {
		region = "us-east-1"
	}
	c, err := b.newClient(region)
	return c, region, err
}

func (b *CloudControlBridge) progress(ctx api.BridgeContext, payload api.BridgePayload, action api.ActionType, started time.Time, msg string) error {
	return ctx.SendStatus(&api.ActionResult{
		UnitID:            payload.UnitID,
		SpaceID:           payload.SpaceID,
		QueuedOperationID: payload.QueuedOperationID,
		ActionResultBaseMeta: api.ActionResultMeta{
			Action:    action,
			Result:    api.ActionResultNone,
			Status:    api.ActionStatusProgressing,
			Message:   msg,
			StartedAt: started,
		},
	})
}

func (b *CloudControlBridge) completed(ctx api.BridgeContext, payload api.BridgePayload, action api.ActionType, result api.ActionResultType, started time.Time, msg string, data, live []byte) error {
	terminated := time.Now()
	return ctx.SendStatus(&api.ActionResult{
		UnitID:            payload.UnitID,
		SpaceID:           payload.SpaceID,
		QueuedOperationID: payload.QueuedOperationID,
		ActionResultBaseMeta: api.ActionResultMeta{
			Action:       action,
			Result:       result,
			Status:       api.ActionStatusCompleted,
			Message:      msg,
			StartedAt:    started,
			TerminatedAt: &terminated,
		},
		Data:     data,
		LiveData: live,
	})
}

func (b *CloudControlBridge) failed(ctx api.BridgeContext, payload api.BridgePayload, action api.ActionType, started time.Time, err error) error {
	terminated := time.Now()
	_ = ctx.SendStatus(&api.ActionResult{
		UnitID:            payload.UnitID,
		SpaceID:           payload.SpaceID,
		QueuedOperationID: payload.QueuedOperationID,
		ActionResultBaseMeta: api.ActionResultMeta{
			Action:       action,
			Result:       api.ActionResultNone,
			Status:       api.ActionStatusFailed,
			Message:      err.Error(),
			StartedAt:    started,
			TerminatedAt: &terminated,
		},
	})
	return err
}

// Apply creates every resource in the unit that has no identifier yet, in
// order, and reads each one back. Resources that already carry an identifier
// are read rather than re-created: Cloud Control has no idempotent create, and
// Floci carries no UpdateResource yet, so convergence of a changed
// desiredState is out of scope for this bridge's first cut — the run
// deploys once and answers questions.
func (b *CloudControlBridge) Apply(ctx api.BridgeContext, payload api.BridgePayload) error {
	started := time.Now()
	if err := b.progress(ctx, payload, api.ActionApply, started, "applying unit through Cloud Control"); err != nil {
		return err
	}
	unit, err := parseUnit(payload.Data)
	if err != nil {
		return b.failed(ctx, payload, api.ActionApply, started, err)
	}
	cc, region, err := b.client(payload)
	if err != nil {
		return b.failed(ctx, payload, api.ActionApply, started, err)
	}

	bg := context.Background()
	for i := range unit.Resources {
		r := &unit.Resources[i]
		if r.Identifier == "" {
			desired, err := desiredJSON(*r)
			if err != nil {
				return b.failed(ctx, payload, api.ActionApply, started, err)
			}
			created, err := cc.CreateResource(bg, &cloudcontrol.CreateResourceInput{
				TypeName:     &r.TypeName,
				DesiredState: &desired,
			})
			if err != nil {
				return b.failed(ctx, payload, api.ActionApply, started,
					fmt.Errorf("%s (%s in %s): %w", r.Label, r.TypeName, region, err))
			}
			identifier, err := b.awaitCreate(bg, cc, created)
			if err != nil {
				return b.failed(ctx, payload, api.ActionApply, started,
					fmt.Errorf("%s (%s in %s): %w", r.Label, r.TypeName, region, err))
			}
			r.Identifier = identifier
		}
		live, err := b.readLive(bg, cc, r.TypeName, r.Identifier)
		if err != nil {
			return b.failed(ctx, payload, api.ActionApply, started,
				fmt.Errorf("%s: created but unreadable: %w", r.Label, err))
		}
		r.Live = live
	}

	rendered, err := renderUnit(unit)
	if err != nil {
		return b.failed(ctx, payload, api.ActionApply, started, err)
	}
	return b.completed(ctx, payload, api.ActionApply, api.ActionResultApplyCompleted, started,
		fmt.Sprintf("applied %d resource(s) in %s", len(unit.Resources), region), rendered, rendered)
}

// awaitCreate polls the request token to a terminal state.
func (b *CloudControlBridge) awaitCreate(bg context.Context, cc ccAPI, created *cloudcontrol.CreateResourceOutput) (string, error) {
	ev := created.ProgressEvent
	for i := 0; i < 60; i++ {
		if ev == nil {
			return "", fmt.Errorf("create returned no progress event")
		}
		switch ev.OperationStatus {
		case cctypes.OperationStatusSuccess:
			if ev.Identifier == nil {
				return "", fmt.Errorf("create succeeded without an identifier")
			}
			return *ev.Identifier, nil
		case cctypes.OperationStatusFailed:
			msg := "create failed"
			if ev.StatusMessage != nil {
				msg = *ev.StatusMessage
			}
			return "", fmt.Errorf("%s", msg)
		}
		time.Sleep(time.Second)
		status, err := cc.GetResourceRequestStatus(bg, &cloudcontrol.GetResourceRequestStatusInput{
			RequestToken: ev.RequestToken,
		})
		if err != nil {
			return "", err
		}
		ev = status.ProgressEvent
	}
	return "", fmt.Errorf("create did not reach a terminal state")
}

func (b *CloudControlBridge) readLive(bg context.Context, cc ccAPI, typeName, identifier string) (map[string]any, error) {
	got, err := cc.GetResource(bg, &cloudcontrol.GetResourceInput{
		TypeName:   &typeName,
		Identifier: &identifier,
	})
	if err != nil {
		return nil, err
	}
	if got.ResourceDescription == nil || got.ResourceDescription.Properties == nil {
		return nil, fmt.Errorf("GetResource returned no properties")
	}
	var live map[string]any
	if err := json.Unmarshal([]byte(*got.ResourceDescription.Properties), &live); err != nil {
		return nil, fmt.Errorf("live properties are not JSON: %w", err)
	}
	return live, nil
}

// Refresh re-reads every resource with an identifier and reports drift by the
// desired-subset rule documented on driftedResources.
func (b *CloudControlBridge) Refresh(ctx api.BridgeContext, payload api.BridgePayload) error {
	started := time.Now()
	if err := b.progress(ctx, payload, api.ActionRefresh, started, "refreshing unit from Cloud Control"); err != nil {
		return err
	}
	unit, err := parseUnit(payload.Data)
	if err != nil {
		return b.failed(ctx, payload, api.ActionRefresh, started, err)
	}
	cc, region, err := b.client(payload)
	if err != nil {
		return b.failed(ctx, payload, api.ActionRefresh, started, err)
	}

	bg := context.Background()
	for i := range unit.Resources {
		r := &unit.Resources[i]
		if r.Identifier == "" {
			r.Live = nil // never applied: drift by definition
			continue
		}
		live, err := b.readLive(bg, cc, r.TypeName, r.Identifier)
		if err != nil {
			r.Live = nil
			continue
		}
		r.Live = live
	}

	rendered, err := renderUnit(unit)
	if err != nil {
		return b.failed(ctx, payload, api.ActionRefresh, started, err)
	}
	drifted := driftedResources(unit)
	result := api.ActionResultRefreshAndNoDrift
	msg := fmt.Sprintf("refreshed %d resource(s) in %s — no drift", len(unit.Resources), region)
	if len(drifted) > 0 {
		result = api.ActionResultRefreshAndDrifted
		msg = fmt.Sprintf("refreshed %d resource(s) in %s — drift on: %s",
			len(unit.Resources), region, strings.Join(drifted, ", "))
	}
	return b.completed(ctx, payload, api.ActionRefresh, result, started, msg, payload.Data, rendered)
}

// Import sweeps ListResources for the requested types and returns what the
// account holds as a unit document — including what no deployment created,
// which is the whole point of asking a store-backed tool that question.
func (b *CloudControlBridge) Import(ctx api.BridgeContext, payload api.BridgePayload) error {
	started := time.Now()
	if err := b.progress(ctx, payload, api.ActionImport, started, "importing live resources through Cloud Control"); err != nil {
		return err
	}
	types := importTypes(payload)
	if len(types) == 0 {
		return b.failed(ctx, payload, api.ActionImport, started,
			fmt.Errorf("nothing to import: no ImportTypes option and the unit declares no types"))
	}
	cc, region, err := b.client(payload)
	if err != nil {
		return b.failed(ctx, payload, api.ActionImport, started, err)
	}

	bg := context.Background()
	var imported Unit
	for _, typeName := range types {
		var next *string
		for {
			out, err := cc.ListResources(bg, &cloudcontrol.ListResourcesInput{
				TypeName:  &typeName,
				NextToken: next,
			})
			if err != nil {
				return b.failed(ctx, payload, api.ActionImport, started,
					fmt.Errorf("%s in %s: %w", typeName, region, err))
			}
			for _, d := range out.ResourceDescriptions {
				r := Resource{TypeName: typeName}
				if d.Identifier != nil {
					r.Identifier = *d.Identifier
					r.Label = labelFor(typeName, *d.Identifier)
				}
				if d.Properties != nil {
					var live map[string]any
					if err := json.Unmarshal([]byte(*d.Properties), &live); err == nil {
						r.Live = live
					}
				}
				imported.Resources = append(imported.Resources, r)
			}
			if out.NextToken == nil {
				break
			}
			next = out.NextToken
		}
	}
	rendered, err := renderUnit(imported)
	if err != nil {
		return b.failed(ctx, payload, api.ActionImport, started, err)
	}
	return b.completed(ctx, payload, api.ActionImport, api.ActionResultImportCompleted, started,
		fmt.Sprintf("imported %d resource(s) across %d type(s) in %s", len(imported.Resources), len(types), region),
		rendered, rendered)
}

func importTypes(payload api.BridgePayload) []string {
	if opts := payload.TargetOptions; opts != nil {
		if raw, ok := opts["ImportTypes"]; ok && strings.TrimSpace(fmt.Sprint(raw)) != "" {
			var types []string
			for _, t := range strings.Split(fmt.Sprint(raw), ",") {
				if t = strings.TrimSpace(t); t != "" {
					types = append(types, t)
				}
			}
			return types
		}
	}
	unit, err := parseUnit(payload.Data)
	if err != nil {
		return nil
	}
	seen := map[string]bool{}
	var types []string
	for _, r := range unit.Resources {
		if !seen[r.TypeName] {
			seen[r.TypeName] = true
			types = append(types, r.TypeName)
		}
	}
	return types
}

func labelFor(typeName, identifier string) string {
	parts := strings.Split(typeName, "::")
	kind := strings.ToLower(parts[len(parts)-1])
	safe := strings.NewReplacer("/", "-", ":", "-", "|", "-").Replace(identifier)
	return kind + "-" + safe
}

// Destroy deletes every resource that carries an identifier, in reverse
// declaration order.
func (b *CloudControlBridge) Destroy(ctx api.BridgeContext, payload api.BridgePayload) error {
	started := time.Now()
	if err := b.progress(ctx, payload, api.ActionDestroy, started, "destroying unit through Cloud Control"); err != nil {
		return err
	}
	unit, err := parseUnit(payload.Data)
	if err != nil {
		return b.failed(ctx, payload, api.ActionDestroy, started, err)
	}
	cc, region, err := b.client(payload)
	if err != nil {
		return b.failed(ctx, payload, api.ActionDestroy, started, err)
	}

	bg := context.Background()
	deleted := 0
	for i := len(unit.Resources) - 1; i >= 0; i-- {
		r := &unit.Resources[i]
		if r.Identifier == "" {
			continue
		}
		if _, err := cc.DeleteResource(bg, &cloudcontrol.DeleteResourceInput{
			TypeName:   &r.TypeName,
			Identifier: &r.Identifier,
		}); err != nil {
			return b.failed(ctx, payload, api.ActionDestroy, started,
				fmt.Errorf("%s (%s in %s): %w", r.Label, r.TypeName, region, err))
		}
		r.Identifier = ""
		r.Live = nil
		deleted++
	}
	rendered, err := renderUnit(unit)
	if err != nil {
		return b.failed(ctx, payload, api.ActionDestroy, started, err)
	}
	return b.completed(ctx, payload, api.ActionDestroy, api.ActionResultDestroyCompleted, started,
		fmt.Sprintf("destroyed %d resource(s) in %s", deleted, region), rendered, rendered)
}

// Finalize has nothing to clean up: the bridge holds no state between calls.
func (b *CloudControlBridge) Finalize(ctx api.BridgeContext, payload api.BridgePayload) error {
	started := time.Now()
	return b.completed(ctx, payload, api.ActionFinalize, api.ActionResultNone, started,
		"nothing to finalize", nil, nil)
}
