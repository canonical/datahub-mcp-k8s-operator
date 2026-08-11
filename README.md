# DataHub MCP K8s Operator

This is the Kubernetes operator for the [DataHub MCP server](https://pypi.org/project/mcp-server-datahub/), the Model Context Protocol interface to the [DataHub](https://datahubproject.io/) metadata catalog.

Full documentation for the Canonical Data Mesh (tutorial, how-to guides, reference, and explanation) lives in the [Canonical Data Mesh documentation](https://github.com/canonical/canonical-data-mesh-docs). Configurations, integrations, and actions are listed on the Charmhub page.

## Description

The Model Context Protocol lets LLM agents call tools on a remote server. The DataHub MCP server exposes the catalog as such a tool set (search, lineage, schemas, dataset queries, glossary) so an agent can answer questions grounded in your catalog instead of in its training data.

Upstream, the server is designed to be launched per user, locally, over stdio, with a personal token on each workstation. This charm runs it once, centrally, as a stateless HTTP endpoint:

- one well-known URL, TLS-terminated at the ingress;
- one DataHub identity as a dedicated read-only service account whose token the DataHub charm provisions and rotates, never a user's;
- one place to authenticate callers, upgrade the workload, and read its logs.

It manages a single container, `mcp-server`, backed by the rock this repository builds from [`rock/`](rock).

## Usage

Note: this operator requires `juju>=3.4` and needs an active [`datahub-k8s`](https://charmhub.io/datahub-k8s) deployment.

```sh
juju deploy datahub-mcp-k8s --channel latest/edge
juju integrate datahub-mcp-k8s datahub-k8s
```

The charm stays `blocked` until DataHub publishes the access token, then goes `active`. The endpoint is then reachable in-cluster at `http://<unit-address>:8000/mcp`, with a health route at `/health`.

Point an MCP client at it:

```json
{
  "mcpServers": {
    "datahub": {
      "url": "https://<your-hostname>/mcp"
    }
  }
}
```

### How the DataHub integration works

On `juju integrate`, the DataHub charm:

1. creates a DataHub **service account** dedicated to this relation, named `[juju] <app>-<relation-id>`;
2. mints a non-expiring Personal Access Token for it;
3. stores the token in a Juju secret granted to the relation, and publishes the GMS URL and the secret ID on the relation.

The token never appears in a databag, in charm config, or in the charm's logs. Removing the relation deletes the service account, which invalidates every token issued for it.

The service account is created with no privileges of its own. It inherits DataHub's default all-users policies, which grant metadata **read** and nothing else so the endpoint cannot write to the catalog even if the mutation tools are switched on. If your deployment has narrowed those default policies, grant the service account a read-only metadata policy in DataHub for the MCP tools to return results.

### Authenticating clients

Without an `oauth` relation the endpoint does not authenticate its callers: anyone who can reach the port can call the tools, and it is up to whatever fronts it to decide who that is. Deploy it that way only on a network where that is true.

Relate an identity provider to close it:

```sh
juju integrate datahub-mcp-k8s nginx-ingress-integrator
juju integrate datahub-mcp-k8s oauth-external-idp-integrator
```

Ingress is required alongside it. The server is an OAuth 2.1 **resource server**: it never runs a login flow, it checks the bearer tokens callers already hold, and clients discover where to get one by reading `/.well-known/oauth-protected-resource` at the server's public URL. Without a public URL there is nothing to advertise, so the charm stays `blocked` until the ingress is ready.

Give the endpoint a hostname of its own and serve it at root:

```sh
juju config nginx-ingress-integrator \
  service-hostname=mcp.example.com path-routes=/ rewrite-enabled=false
```

It stays `blocked` until the provider has registered the client too. Serving in that window would leave a public endpoint open, so the charm stops the workload rather than run it unauthenticated.

A user then authenticates as themselves at the identity provider since the MCP client runs the browser flow and presents the resulting token. The charm accepts a token only if the provider confirms it and it was issued for this deployment. What a caller sees in the catalog does not depend on who they are: every call reaches DataHub as the one service account from the `datahub-client` relation. The identity decides whether you may call the server at all, not what it will show you.

Two dialects are supported, chosen from what the provider publishes:

- **signed tokens**, checked locally against the provider's JWKS;
- **unsigned tokens**, checked by asking the provider: the standard introspection endpoint, or Google's `tokeninfo` endpoint, which is the only way to check a Google token.

### Mutation tools

The read-only tool set (`search`, `get_lineage`, `get_entities`, `list_schema_fields`, `get_dataset_queries`, `get_lineage_paths_between`) is what the charm serves by default.

`enable-mutation-tools=true` additionally exposes the tools that write tags, glossary terms, owners, domains and descriptions. Every write is attributed to the shared service account rather than to the user whose agent made the call, and the service account holds no write privileges by default, so this is off unless you have decided you want it and granted those privileges in DataHub.

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines on enhancements to this charm following best practice guidelines, and [CONTRIBUTING.md](CONTRIBUTING.md) for developer guidance.

## License

The Charmed DataHub MCP K8s Operator is free software, distributed under the Apache Software License, version 2.0. See [License](LICENSE) for more details.
