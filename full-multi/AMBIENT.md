# Ambient-mode deployment

The `multi-ambient` branch deploys a separate Argo CD Application named
`multi-ambient` into namespace `multi-ambient`. The existing `full-multi`
Application and namespace are independent. This deployment provisions new
credentials and four MariaDB PVCs and seeds them from the existing images; it
does not copy the existing databases.

The cluster already provides ambient Service Mesh, OpenTelemetry and Tempo.
Before deploying, verify the ambient CNI and a Ready ZTunnel cover application
nodes, Gateway API CRDs and the `istio` and `istio-waypoint` GatewayClasses
are available, and the existing `otel-tracing` provider is configured. The
application workflow does not install or modify these shared services.

The cluster-scoped RolloutManager must configure the Gateway API traffic
plugin. Preflight requires its `v0.17.0` release URL: its weight handling
modifies only rules containing both relevant stable and canary Services, which
is necessary for the shared Front HTTPRoute. The controller service account
needs get/list/update/patch on HTTPRoutes and get on Services in
`multi-ambient`. Preflight checks these permissions using `oc auth can-i --as`,
which requires impersonation permission. Confirm the plugin is actually
loaded; configuration and RBAC alone do not prove that it is running. See the
[plugin installation documentation](https://rollouts-plugin-trafficrouter-gatewayapi.readthedocs.io/en/v0.17.0/installation/).

1. Publish the reviewed `multi-ambient` branch. Check that the independent
   Application name is available and that server-side admission accepts the
   rendered manifests. The platform owner must configure any missing shared
   prerequisites.
2. Follow [GLOBAL-WORKFLOWS.md](GLOBAL-WORKFLOWS.md) to install GitOps and
   Rollouts prerequisites and provision the new namespace, Secrets, four
   independent databases and the GitOps Application. For disposable demo
   credentials the Secrets script can generate random values; existing Secrets
   are never rotated automatically.
3. Confirm ambient labels and the absence of sidecar injection labels, then
   inspect Gateway and HTTPRoute status, Rollouts, PVCs, and endpoint traffic.
   Verify the waypoint and ZTunnel, Kiali metrics, and new Tempo traces. Test
   every forced Front, Backend, and CRUD track and the canary weights.
4. Establish the Blue baseline and start A and B only when the ambient data
   paths are healthy. Promote candidates only after confirming the intended
   revision. Do not infer working routing from GitOps Synced alone.

Local tests validate manifest structure and routing selection. They do not
establish live Gateway reconciliation, plugin operation, observability or
end-to-end traffic on the cluster.
