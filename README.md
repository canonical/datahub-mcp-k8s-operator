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
      "type": "http",
      "url": "https://<your-hostname>/mcp"
    }
  }
}
```

The URL is all a client ever needs, including when the endpoint authenticates its callers: it discovers where to authenticate and obtains its own credentials on its own. No client ID, secret or callback port belongs in a client's configuration. The exception is a deployment that has [limited itself to callers registered in advance](#limiting-the-endpoint-to-callers-you-registered), where an operator hands the client its credentials instead.

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

Ingress is required alongside it, and it must serve **HTTPS**: an OAuth issuer identifier cannot be an `http://` URL, and the charm blocks rather than let the workload fail to start. Without a public URL there is nothing to advertise at all, so it blocks then too.

Give the endpoint a hostname of its own and serve it at root:

```sh
juju config nginx-ingress-integrator \
  service-hostname=mcp.example.com path-routes=/ rewrite-enabled=false
```

It stays `blocked` until the provider has registered the client too. Serving in that window would leave a public endpoint open, so the charm stops the workload rather than run it unauthenticated.

A user then authenticates as themselves at the identity provider, and the charm accepts the resulting token only if the provider confirms it and it was issued for this deployment. What a caller sees in the catalog does not depend on who they are: every call reaches DataHub as the one service account from the `datahub-client` relation. The identity decides whether you may call the server at all, not what it will show you.

#### Providers that register clients themselves

Against an identity provider that publishes a `registration_endpoint` (e.g.,Ory Hydra, and so the Canonical IdP) the server is a plain OAuth 2.1 **resource server**. It runs no login flow of its own; it checks the bearer tokens callers arrive with, and points them at the provider through `/.well-known/oauth-protected-resource`. Tokens are checked either against the provider's JWKS when it signs them, or by asking its introspection endpoint when it does not.

#### Google

Google publishes no registration endpoint, so a client pointed at it has no way to obtain credentials, and every user would otherwise need their own OAuth client. The charm therefore runs an **OAuth proxy** in front of Google: an authorization server of its own that registers callers on demand and holds the single Google client the deployment owns. Callers see a standard, fully self-configuring OAuth server; Google sees one registered application.

This also fixes the redirect URI. MCP clients listen on an ephemeral loopback port that changes per attempt, which can never match a pre-registered entry. With the proxy, Google only ever redirects to the server's own fixed callback, and the proxy forwards to whichever port the caller chose.

Not every caller can use the proxy. A client that reads `/.well-known/oauth-protected-resource` finds it and configures itself; one that is set up by hand from a form never looks, and is given Google's own endpoints instead. That client authenticates directly at Google and arrives holding a Google token rather than one the proxy minted, so the server also accepts a token Google confirms was issued to the client this deployment owns. [Gemini Enterprise](#gemini-enterprise) connects that way, and its connector offers no other.

In the Google Cloud console, per environment:

1. Create an OAuth client of type **Web application**. A desktop client is not needed, the secret stays on the server and never reaches users.
2. Add `https://<your-hostname>/auth/callback` as an authorised redirect URI. Add the redirect URI of any client configured by hand alongside it, since it authenticates at Google directly rather than through the server's callback.
3. Put that client's ID and secret on the `oauth-external-idp-integrator` for this deployment. Redirect URIs are per client, so this must be an integrator carrying that client, not one shared with another application.

Two consequences worth knowing:

- **The workload needs egress to `oauth2.googleapis.com`**, because it exchanges the authorization code and validates tokens server-side. Behind a filtering proxy, allowlist that host; the charm forwards the model's `juju-http-proxy`, `juju-https-proxy` and `juju-no-proxy` settings to the workload.
- **Run a single unit.** The proxy is the authorization server, and it holds its client registrations and issued tokens in the unit, so a second unit would not recognise the first one's tokens and a restarted unit does not recognise its own. Callers that discover the proxy sign in again when that happens; a caller pointed at Google by hand holds a token no unit had to issue and is unaffected. Providers that register clients themselves have no such state and scale normally.

ClientID Metadata Documents (CIMD), where a caller names a URL it hosts instead of registering, are deliberately not offered. Serving them means fetching a URL the caller chooses, from a domain that differs per client which a deployment behind a filtering egress proxy cannot do. Registration requires no outbound call and works everywhere.

#### Limiting the endpoint to callers you registered

By default a caller may obtain an OAuth client of its own. That is how an MCP client on a developer's machine connects with nothing configured but the URL, and it means anyone who can reach the endpoint and authenticate at the identity provider can call the tools.

`enable-client-registration=false` withdraws that, leaving only the callers an operator set up in advance by giving them this deployment's own client ID and secret:

```sh
juju config datahub-mcp-k8s enable-client-registration=false
```

A caller configured that way needs no charm configuration of its own. It presents the client this deployment already holds, which the server recognises without a registration.

Where the rule is enforced follows from who does the registering:

- **Fronting Google**, this server is the registrar. It stops serving `/register`, stops advertising it in the authorization server metadata, and resolves no client but its own, so callers that registered while it was on are cut off along with new ones. A caller holding a Google token is held to the same rule by the client Google names on it.
- **Against a provider that registers clients itself**, registration happens at the provider and the charm cannot stop it. It instead refuses any token whose `client_id` or `azp` claim is not this deployment's client.

#### Gemini Enterprise

Gemini Enterprise's custom MCP connector does no discovery: an admin types the endpoints into a form. Give it **Google's** endpoints, not this server's, because that is where such a client authenticates:

| Field | Value |
| --- | --- |
| MCP Server URL | `https://<your-hostname>/mcp` |
| Authorization URL | `https://accounts.google.com/o/oauth2/auth` |
| Token URL | `https://oauth2.googleapis.com/token` |
| Client ID / Client Secret | The same Google client that is on the `oauth` relation |

The client that deployment owns must carry `https://vertexaisearch.cloud.google.com/oauth-redirect` among its authorised redirect URIs, which is where Google sends the user back to Gemini. Nothing else on the charm needs changing, and this works whether or not client registration is enabled: what admits the caller is the client its token names, which is the one an operator gave it.

### Mutation tools

The read-only tool set (`search`, `get_lineage`, `get_entities`, `list_schema_fields`, `get_dataset_queries`, `get_lineage_paths_between`) is what the charm serves by default.

`enable-mutation-tools=true` additionally exposes the tools that write tags, glossary terms, owners, domains and descriptions. Every write is attributed to the shared service account rather than to the user whose agent made the call, and the service account holds no write privileges by default, so this is off unless you have decided you want it and granted those privileges in DataHub.

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines on enhancements to this charm following best practice guidelines, and [CONTRIBUTING.md](CONTRIBUTING.md) for developer guidance.

## License

The Charmed DataHub MCP K8s Operator is free software, distributed under the Apache Software License, version 2.0. See [License](LICENSE) for more details.
