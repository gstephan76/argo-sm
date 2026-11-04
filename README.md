# argo-sm — OpenShift Service Mesh, Argo CD and Argo Rollouts demos

- [full-multi application and demo lifecycle](full-multi/README.md)
- [Full-multi fresh-cluster bootstrap, observability and troubleshooting](full-multi/BOOTSTRAP.md)
- [Canary mesh full demo](full-demo/README.md)

The repository contains application GitOps sources. Shared platform components
(Istio control plane, Argo Rollouts controller, OpenTelemetry Collector, Tempo,
Kiali) are external prerequisites and should be managed by their platform
owners. Do not conflate Argo CD `Synced` with Rollouts readiness or distributed
tracing health.
