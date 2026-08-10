// The confighub arm's worker: one bridge, Cloud Control, region targets.
//
// Run it the way the SDK's bridge guide runs its example:
//
//	cub worker run --space $SPACE --executable ./bridge confighub-cc
//
// or with the env set by `cub worker get-envs`. BRIDGE_REGIONS narrows the
// targets (default: the scenario's three regions); AWS_ENDPOINT_URL points
// the applies at an emulator.
package main

import (
	"log"
	"os"
	"strings"

	"github.com/confighub/sdk/core/worker"
)

func main() {
	regions := []string{"us-east-1", "us-west-1", "us-west-2"}
	if raw := os.Getenv("BRIDGE_REGIONS"); raw != "" {
		regions = nil
		for _, r := range strings.Split(raw, ",") {
			if r = strings.TrimSpace(r); r != "" {
				regions = append(regions, r)
			}
		}
	}

	dispatcher := worker.NewBridgeDispatcher()
	dispatcher.RegisterBridge(NewCloudControlBridge(regions))

	connector, err := worker.NewConnector(worker.ConnectorOptions{
		WorkerID:         os.Getenv("CONFIGHUB_WORKER_ID"),
		WorkerSecret:     os.Getenv("CONFIGHUB_WORKER_SECRET"),
		ConfigHubURL:     os.Getenv("CONFIGHUB_URL"),
		BridgeDispatcher: &dispatcher,
	})
	if err != nil {
		log.Fatalf("connector: %v", err)
	}
	if err := connector.Start(); err != nil {
		log.Fatalf("connector: %v", err)
	}
}
