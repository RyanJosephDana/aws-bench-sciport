package main

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/cloudcontrol"
	cctypes "github.com/aws/aws-sdk-go-v2/service/cloudcontrol/types"
	"github.com/confighub/sdk/core/worker/api"
)

// fakeCC serves the slice of Cloud Control the bridge calls, in memory.
type fakeCC struct {
	created map[string]string // identifier -> properties JSON
	nextID  int
}

func newFakeCC() *fakeCC { return &fakeCC{created: map[string]string{}} }

func (f *fakeCC) CreateResource(_ context.Context, in *cloudcontrol.CreateResourceInput, _ ...func(*cloudcontrol.Options)) (*cloudcontrol.CreateResourceOutput, error) {
	f.nextID++
	id := fmt.Sprintf("vpc-%08d", f.nextID)
	f.created[id] = *in.DesiredState
	return &cloudcontrol.CreateResourceOutput{ProgressEvent: &cctypes.ProgressEvent{
		OperationStatus: cctypes.OperationStatusSuccess,
		Identifier:      aws.String(id),
		RequestToken:    aws.String("tok"),
	}}, nil
}

func (f *fakeCC) GetResource(_ context.Context, in *cloudcontrol.GetResourceInput, _ ...func(*cloudcontrol.Options)) (*cloudcontrol.GetResourceOutput, error) {
	props, ok := f.created[*in.Identifier]
	if !ok {
		return nil, fmt.Errorf("ResourceNotFoundException: %s", *in.Identifier)
	}
	// The service reports the identifier alongside what was asked for,
	// the way Cloud Control's read model does.
	merged := strings.TrimSuffix(props, "}") + `,"VpcId":"` + *in.Identifier + `"}`
	if props == "{}" {
		merged = `{"VpcId":"` + *in.Identifier + `"}`
	}
	return &cloudcontrol.GetResourceOutput{ResourceDescription: &cctypes.ResourceDescription{
		Identifier: in.Identifier,
		Properties: aws.String(merged),
	}}, nil
}

func (f *fakeCC) DeleteResource(_ context.Context, in *cloudcontrol.DeleteResourceInput, _ ...func(*cloudcontrol.Options)) (*cloudcontrol.DeleteResourceOutput, error) {
	delete(f.created, *in.Identifier)
	return &cloudcontrol.DeleteResourceOutput{}, nil
}

func (f *fakeCC) ListResources(_ context.Context, _ *cloudcontrol.ListResourcesInput, _ ...func(*cloudcontrol.Options)) (*cloudcontrol.ListResourcesOutput, error) {
	out := &cloudcontrol.ListResourcesOutput{}
	for id, props := range f.created {
		out.ResourceDescriptions = append(out.ResourceDescriptions, cctypes.ResourceDescription{
			Identifier: aws.String(id),
			Properties: aws.String(props),
		})
	}
	return out, nil
}

func (f *fakeCC) GetResourceRequestStatus(_ context.Context, _ *cloudcontrol.GetResourceRequestStatusInput, _ ...func(*cloudcontrol.Options)) (*cloudcontrol.GetResourceRequestStatusOutput, error) {
	return nil, fmt.Errorf("not used: creates complete synchronously in the fake")
}

// recorderCtx captures the statuses a bridge sends.
type recorderCtx struct{ results []*api.ActionResult }

func (r *recorderCtx) SendStatus(res *api.ActionResult) error {
	r.results = append(r.results, res)
	return nil
}

func (r *recorderCtx) last() *api.ActionResult { return r.results[len(r.results)-1] }

func (r *recorderCtx) Context() context.Context { return context.Background() }

func (r *recorderCtx) GetServerURL() string { return "http://recorder.invalid" }

func (r *recorderCtx) GetWorkerID() string { return "recorder" }

func bridgeOver(cc ccAPI) *CloudControlBridge {
	b := NewCloudControlBridge([]string{"us-east-1"})
	b.newClient = func(string) (ccAPI, error) { return cc, nil }
	return b
}

const unitYAML = `resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    desiredState:
      CidrBlock: 10.42.0.0/16
`

func TestApplyCreatesAndRecordsIdentifiers(t *testing.T) {
	cc := newFakeCC()
	b := bridgeOver(cc)
	ctx := &recorderCtx{}

	err := b.Apply(ctx, api.BridgePayload{Data: []byte(unitYAML), BridgeHandle: "us-east-1"})
	if err != nil {
		t.Fatalf("apply: %v", err)
	}
	last := ctx.last()
	if last.Result != api.ActionResultApplyCompleted {
		t.Fatalf("result = %v, want apply-completed (%s)", last.Result, last.Message)
	}
	unit, err := parseUnit(last.LiveData)
	if err != nil {
		t.Fatalf("live data: %v", err)
	}
	if unit.Resources[0].Identifier == "" {
		t.Fatal("apply recorded no identifier")
	}
	if unit.Resources[0].Live["CidrBlock"] != "10.42.0.0/16" {
		t.Fatalf("live read not recorded: %v", unit.Resources[0].Live)
	}
	if len(cc.created) != 1 {
		t.Fatalf("created %d resources, want 1", len(cc.created))
	}
}

func TestApplyIsReadOnlyForAlreadyIdentifiedResources(t *testing.T) {
	cc := newFakeCC()
	cc.created["vpc-existing"] = `{"CidrBlock":"10.0.0.0/16"}`
	b := bridgeOver(cc)
	ctx := &recorderCtx{}

	unit := `resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    identifier: vpc-existing
    desiredState:
      CidrBlock: 10.0.0.0/16
`
	if err := b.Apply(ctx, api.BridgePayload{Data: []byte(unit)}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if len(cc.created) != 1 {
		t.Fatalf("re-apply created a duplicate: %d resources", len(cc.created))
	}
}

func TestRefreshReportsDriftBySubsetRule(t *testing.T) {
	cc := newFakeCC()
	cc.created["vpc-live"] = `{"CidrBlock":"10.99.0.0/16"}`
	b := bridgeOver(cc)

	drifted := `resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    identifier: vpc-live
    desiredState:
      CidrBlock: 10.42.0.0/16
`
	ctx := &recorderCtx{}
	if err := b.Refresh(ctx, api.BridgePayload{Data: []byte(drifted)}); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	if ctx.last().Result != api.ActionResultRefreshAndDrifted {
		t.Fatalf("desired 10.42/16 vs live 10.99/16 read as %v, want drifted", ctx.last().Result)
	}

	clean := `resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    identifier: vpc-live
    desiredState:
      CidrBlock: 10.99.0.0/16
`
	ctx = &recorderCtx{}
	if err := b.Refresh(ctx, api.BridgePayload{Data: []byte(clean)}); err != nil {
		t.Fatalf("refresh: %v", err)
	}
	// The live side also carries VpcId, which the declared side never states —
	// live-only keys must not read as drift.
	if ctx.last().Result != api.ActionResultRefreshAndNoDrift {
		t.Fatalf("matching desired state read as %v: %s", ctx.last().Result, ctx.last().Message)
	}
}

func TestImportSweepsDeclaredTypes(t *testing.T) {
	cc := newFakeCC()
	cc.created["vpc-default"] = `{"CidrBlock":"172.31.0.0/16"}`
	cc.created["vpc-other"] = `{"CidrBlock":"10.1.0.0/16"}`
	b := bridgeOver(cc)
	ctx := &recorderCtx{}

	if err := b.Import(ctx, api.BridgePayload{Data: []byte(unitYAML)}); err != nil {
		t.Fatalf("import: %v", err)
	}
	unit, err := parseUnit(ctx.last().Data)
	if err != nil {
		t.Fatalf("imported data: %v", err)
	}
	if len(unit.Resources) != 2 {
		t.Fatalf("imported %d resources, want the account's 2", len(unit.Resources))
	}
}

func TestDestroyDeletesInReverseAndClearsIdentifiers(t *testing.T) {
	cc := newFakeCC()
	cc.created["vpc-a"] = `{}`
	b := bridgeOver(cc)
	ctx := &recorderCtx{}

	unit := `resources:
  - label: gateVpc
    typeName: AWS::EC2::VPC
    identifier: vpc-a
    desiredState: {}
`
	if err := b.Destroy(ctx, api.BridgePayload{Data: []byte(unit)}); err != nil {
		t.Fatalf("destroy: %v", err)
	}
	if len(cc.created) != 0 {
		t.Fatalf("resource survived destroy")
	}
	out, _ := parseUnit(ctx.last().Data)
	if out.Resources[0].Identifier != "" {
		t.Fatal("identifier not cleared after destroy")
	}
}
