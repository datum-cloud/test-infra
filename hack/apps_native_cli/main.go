// This test-only entry point is copied into a private clone of pinned datumctl.
// It invokes the exported Apps Command with a scoped kubeconfig, without using
// datumctl's interactive root OAuth factory. It never accepts a bearer token.
package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"time"

	appscmd "go.datum.net/datumctl/internal/cmd/apps"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const projectPrefix = "/apis/resourcemanager.miloapis.com/v1alpha1/projects/"

func scopedConfig(path, project string, port int) (*rest.Config, error) {
	if len(validation.IsDNS1123Subdomain(project)) != 0 || project == "" || port < 1 || port > 65535 {
		return nil, fmt.Errorf("invalid exact Project or private port")
	}
	if !filepath.IsAbs(path) {
		return nil, fmt.Errorf("absolute scoped kubeconfig path required")
	}
	raw, err := clientcmd.LoadFromFile(path)
	if err != nil || len(raw.Clusters) != 1 || len(raw.Contexts) != 1 || len(raw.AuthInfos) != 1 {
		return nil, fmt.Errorf("one scoped kubeconfig context, cluster and user required")
	}
	selected, ok := raw.Contexts[raw.CurrentContext]
	if !ok || selected == nil || raw.Clusters[selected.Cluster] == nil || raw.AuthInfos[selected.AuthInfo] == nil {
		return nil, fmt.Errorf("explicit scoped kubeconfig context required")
	}
	identity := raw.AuthInfos[selected.AuthInfo]
	if identity.Token == "" || identity.TokenFile != "" || identity.Username != "" ||
		identity.Password != "" || identity.ClientCertificate != "" ||
		identity.ClientCertificateData != nil || identity.ClientKey != "" ||
		identity.ClientKeyData != nil || identity.Exec != nil || identity.AuthProvider != nil ||
		identity.Impersonate != "" || len(identity.ImpersonateGroups) != 0 ||
		len(identity.ImpersonateUserExtra) != 0 {
		return nil, fmt.Errorf("one bearer identity without alternate credentials required")
	}
	config, err := clientcmd.BuildConfigFromFlags("", path)
	if err != nil {
		return nil, fmt.Errorf("scoped kubeconfig is unavailable")
	}
	u, err := url.Parse(config.Host)
	wantPath := projectPrefix + project + "/control-plane"
	if err != nil || u.Scheme != "https" || u.Hostname() != "127.0.0.1" ||
		u.Port() != strconv.Itoa(port) || u.Path != wantPath ||
		u.RawQuery != "" || u.Fragment != "" || u.User != nil || u.Opaque != "" ||
		config.BearerToken == "" || config.BearerTokenFile != "" ||
		len(config.CAData) == 0 || config.Insecure || config.ServerName != "" ||
		config.ExecProvider != nil || config.AuthProvider != nil ||
		len(config.CertData) != 0 || len(config.KeyData) != 0 {
		return nil, fmt.Errorf("kubeconfig must hold one CA-verified loopback Project API and scoped bearer identity")
	}
	return config, nil
}

func run() error {
	flags := flag.NewFlagSet("apps-native-cli", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	kubeconfig := flags.String("kubeconfig", "", "absolute scoped kubeconfig file")
	project := flags.String("project", "", "exact Milo Project name")
	port := flags.Int("port", 0, "private loopback Milo port")
	if err := flags.Parse(os.Args[1:]); err != nil || *kubeconfig == "" || len(flags.Args()) == 0 {
		return fmt.Errorf("kubeconfig, Project, port and Apps subcommand are required")
	}
	config, err := scopedConfig(*kubeconfig, *project, *port)
	if err != nil {
		return err
	}
	var output bytes.Buffer
	command := appscmd.Command(appscmd.Options{
		RESTConfig: func() (*rest.Config, error) { return rest.CopyConfig(config), nil },
		Out:        &output,
	})
	command.SetArgs(flags.Args())
	command.SetOut(&output)
	command.SetErr(io.Discard)
	command.SilenceErrors = true
	command.SilenceUsage = true
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	if err := command.ExecuteContext(ctx); err != nil {
		return fmt.Errorf("exported datumctl Apps Command failed")
	}
	if output.Len() > 1<<16 {
		return fmt.Errorf("Apps Command output exceeded test bound")
	}
	_, err = os.Stdout.Write(output.Bytes())
	return err
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
