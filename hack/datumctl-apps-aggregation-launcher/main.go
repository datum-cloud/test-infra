// Command datumctl-apps-aggregation-launcher invokes the actual exported Apps
// command tree. Its small wrapper supplies only test kubeconfig and project
// flags; it does not implement authorization or replace Apps command behavior.
package main

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
	appscmd "go.datum.net/datumctl/internal/cmd/apps"
	"go.datum.net/datumctl/internal/miloapi"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func main() {
	project := ""
	root := &cobra.Command{Use: "datumctl", SilenceUsage: true, SilenceErrors: true}
	root.PersistentFlags().StringVar(&project, "project", "", "project scope for the authoring API")
	root.AddCommand(appscmd.Command(appscmd.Options{
		RESTConfig: func() (*rest.Config, error) {
			if project == "" {
				return nil, fmt.Errorf("--project is required")
			}
			cfg, err := clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
				clientcmd.NewDefaultClientConfigLoadingRules(), &clientcmd.ConfigOverrides{},
			).ClientConfig()
			if err != nil {
				return nil, err
			}
			cfg.Host = miloapi.ProjectControlPlaneURL(cfg.Host, project)
			return cfg, nil
		},
		Out: os.Stdout,
	}))
	if err := root.Execute(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
