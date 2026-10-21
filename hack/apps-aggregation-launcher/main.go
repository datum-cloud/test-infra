// Command apps-aggregation-launcher starts the pinned Milo API server through
// its normal Options/Complete/Validate/Run path. It only adds the egress
// selector binding needed by the loopback aggregation test; it does not
// replace Milo authentication, authorization, admission, or APIService proxying.
package main

import (
	"context"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	"github.com/spf13/pflag"
	app "go.miloapis.com/milo/cmd/milo/apiserver"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	utilerrors "k8s.io/apimachinery/pkg/util/errors"
	apiservercompat "k8s.io/apiserver/pkg/util/compatibility"
	utilfeature "k8s.io/apiserver/pkg/util/feature"
	"k8s.io/component-base/cli/flag"
	basecompatibility "k8s.io/component-base/compatibility"
	logsapi "k8s.io/component-base/logs/api/v1"
)

func main() {
	app.SystemNamespace = "milo-system"
	// Milo's Run applies these package-level settings. Initialize them through
	// the same flags registered by Milo's command before using the Options path.
	cmd := app.NewCommand()
	for name, value := range map[string]string{
		"watch-cache-consistency-check-period": "1h",
		"shared-etcd-client-pool-size":         "32",
	} {
		if err := cmd.Flags().Set(name, value); err != nil {
			log.Fatal(err)
		}
	}

	opts := app.NewOptions()
	opts.GenericServerRunOptions.ComponentGlobalsRegistry = basecompatibility.NewComponentGlobalsRegistry()
	opts.GenericServerRunOptions.ComponentGlobalsRegistry.ComponentGlobalsOrRegister(basecompatibility.DefaultKubeComponent, apiservercompat.DefaultBuildEffectiveVersion(), utilfeature.DefaultMutableFeatureGate)
	var named flag.NamedFlagSets
	opts.AddFlags(&named)
	fs := pflag.NewFlagSet("milo-aggregation-probe", pflag.ContinueOnError)
	for _, set := range named.FlagSets {
		fs.AddFlagSet(set)
	}
	if fs.Lookup("egress-selector-config-file") == nil {
		opts.EgressSelector.AddFlags(fs)
	}
	if err := fs.Parse(os.Args[1:]); err != nil {
		log.Fatal(err)
	}
	if opts.EgressSelector == nil || opts.EgressSelector.ConfigFile == "" {
		log.Fatal("egress selector configuration was not bound to Milo options")
	}
	log.Printf("using egress selector config %s", opts.EgressSelector.ConfigFile)
	opts.SystemNamespaces = []string{metav1.NamespaceSystem, metav1.NamespaceDefault, app.SystemNamespace}
	if err := logsapi.ValidateAndApply(opts.Logs, utilfeature.DefaultFeatureGate); err != nil {
		log.Fatal(err)
	}
	completed, err := opts.Complete(context.Background(), []string{}, []net.IP{})
	if err != nil {
		log.Fatal(err)
	}
	if err := utilfeature.DefaultMutableFeatureGate.Set("APIServerTracing=true"); err != nil {
		log.Fatal(err)
	}
	if errs := completed.Validate(); len(errs) != 0 {
		log.Fatal(utilerrors.NewAggregate(errs))
	}
	utilfeature.DefaultMutableFeatureGate.AddMetrics()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := app.Run(ctx, completed); err != nil {
		log.Fatal(err)
	}
}
