# mcp-aws

A read-only AWS MCP server for stdio, designed to sit behind an MCP gateway.

## The problem it solves

One tool per AWS API operation gives you hundreds of tools. Every tool schema is resent
to the model on every turn, so a broad AWS server spends the context budget before the
first question is asked.

`mcp-aws` registers **five tools, permanently**, over a catalog of read-only *views*
declared in YAML. Coverage grows by adding YAML; the tool surface does not move. The
catalog is discovered at runtime, so a model pays for the one view it needs instead of
carrying the whole of AWS.

```
aws_list_accounts    which profiles (accounts) are configured and usable
aws_catalog          view ids + one-line summaries, filterable by service or search
aws_describe_view    one view's parameters and output shape, on demand
aws_query            run a view against a profile and region
aws_read_resource    ARN in, the matching detail view out
```

Typical flow: `aws_catalog(service="eks")` → `aws_describe_view("eks.nodegroups.list")`
→ `aws_query(...)`.

## Install and run

```bash
uv sync
uv run mcp-aws          # speaks MCP over stdio
```

Gateway / client config:

```json
{
  "mcpServers": {
    "aws": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-aws", "mcp-aws"],
      "env": { "AWS_CONFIG_FILE": "/Users/you/.aws/config" }
    }
  }
}
```

## Credentials

Each **named profile** in your AWS config is treated as an account. Nothing is assumed
or assume-roled on your behalf. `aws_list_accounts` resolves every profile concurrently
and reports per-profile status, so one expired SSO session degrades that row rather than
the call:

```json
{"profile": "prod", "account_id": "123456789012", "account_alias": "acme-prod",
 "default_region": "us-east-1", "status": "ok"}
```

## Read-only by construction

Read-only is enforced at catalog load, not at call time:

1. The operation name must match a read-only prefix (`describe_`, `list_`, `get_`, …).
2. The operation must exist in the botocore service model, and every declared parameter
   must be a real member of its request shape.
3. Read-shaped but sensitive operations (`s3:get_object`, `secretsmanager:get_secret_value`,
   `ssm:get_parameter`, `kms:decrypt`, log and item reads …) are denylisted outright.

A view that fails any of these aborts startup. The engine can only ever invoke an
operation that survived, so there is no code path from a tool call to a mutating API.

## Adding coverage

Add an entry to a file in `src/mcp_aws/catalog/views/`. No Python, no new tool, no
change to the context cost:

```yaml
- id: ec2.vpcs.list
  summary: VPCs with CIDR blocks and default flag
  client: ec2                 # boto3 client name
  operation: describe_vpcs    # snake_case, must be read-only and real
  paginate: true
  params:
    vpc_ids:
      type: "string[]"
      description: Specific VPC ids.
      maps_to: VpcIds         # request member, 'A.B' nesting, or 'Filters[vpc-id]'
  project: |                  # JMESPath, applied per page
    Vpcs[].{id: VpcId, cidr: CidrBlock, is_default: IsDefault}
  returns: One object per VPC.
  detail_of: ec2:vpc          # optional: makes this the target of aws_read_resource
  detail_param: vpc_ids
```

`uv run pytest tests/test_catalog.py` validates every view against the real AWS models.

Two constructs cover the awkward APIs:

- **`expand`** — a declarative fan-out for services that split a list from its detail
  (`eks:list_clusters` + `describe_cluster`). `inherit` carries the parent's key into
  the child call, for `describe_nodegroup` and friends that need `clusterName` too.
  Bounded by `max_items` and a small thread pool.
- **`Filters[$param]`** — a filter whose *key* is user data rather than part of the API
  contract, as the Resource Groups Tagging API needs. `$self` means this parameter's own
  value is the key; `$other` takes the key from a sibling parameter.

Point `MCP_AWS_CATALOG_DIR` at your own directory to add or override views without
forking the package.

## Results and pagination

Every response uses one envelope:

```json
{"view": "ec2.instances.list", "profile": "prod", "region": "us-east-1",
 "count": 100, "truncated": true, "next_cursor": "eyJ0Ijo…", "items": [...]}
```

Results are capped by item count *and* serialized size. Truncation is lossless: each
item records the page it came from, so a cut landing in the middle of an AWS page still
produces a cursor that resumes at exactly the first dropped item.

## Resources

Tools are the path a model uses; resources are for humans and for clients that browse,
`@`-mention, or build a picker out of them.

Each **listing** is a JSON Schema enum fragment naming the template one of its values
buys, which is what an MCP gateway needs to turn a set into clickable values. Listings are
read whenever a client opens the server, so they never call AWS.

| Listing | Values | Buys |
|---|---|---|
| `aws://accounts` | configured profiles | `aws://accounts/{profile}` |
| `aws://catalog` | every view id, labelled with its summary | `aws://catalog/{view_id}` |
| `aws://services` | services the catalog covers | narrows to `aws://services/{service}/views` |
| `aws://services/{service}/views` | one service's view ids | `aws://catalog/{view_id}` |
| `aws://regions` | regions, from bundled endpoint data | `aws://regions/{region}` |

| Member | Contents | Cost |
|---|---|---|
| `aws://accounts/{profile}` | one profile resolved to account, alias, status | live STS/IAM |
| `aws://catalog/{view_id}` | one view's parameters and output shape | free |
| `aws://regions/{region}` | partition, geography, profiles defaulting there | free |
| `aws://query/{profile}/{region}/{view_id}` | a view result (`default` as region = the profile's own) | **live AWS read** |

`aws://query/...` pairs with no listing on purpose: a listing beside it would be read on
open, and reading it would mean running queries nobody asked for.

> **Breaking change in this release:** the result template was `aws://{profile}/{region}/{view_id}`.
> Its fixed prefix was `aws://`, which shadowed every other URI. It is now
> `aws://query/{profile}/{region}/{view_id}`, and `aws://catalog` returns a vocabulary
> rather than a full dump — the dump is `aws_catalog` and `aws://catalog/{view_id}`.

## Behind a gateway

Everything in this section is ordinary MCP; a client that ignores it loses nothing.

- **Injectable values.** The listing/template pairs above publish the four argument names
  worth picking rather than typing — `profile`, `region`, `view_id` and `service`. A
  gateway's admin UI renders each as chips that fill the matching field.
- **Completions.** `completion/complete` answers for those same four, filtered by what has
  been typed and narrowed by the sibling fields already filled: a `view_id` asked for
  beside a chosen `service` offers only that service's views.
- **Prompts.** `aws_account_overview(profile, region)` and
  `aws_investigate_resource(arn, profile)`. Their argument names are the vocabulary's
  variables, which is what lets the same chips fill them.

None of this touches the tool surface: resources, templates, completions and prompts are
fetched on demand instead of being resent every turn.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MCP_AWS_TOOL_PREFIX` | `aws_` | Namespace the tools behind a gateway |
| `MCP_AWS_PROFILES` | all | Comma-separated allowlist of visible profiles |
| `MCP_AWS_CATALOG_DIR` | – | Extra catalog directories (later wins) |
| `MCP_AWS_MAX_ITEMS` | `100` | Default item cap per response |
| `MCP_AWS_MAX_CHARS` | `20000` | Serialized size cap per response |
| `MCP_AWS_CACHE_TTL` | `60` | Result cache seconds; `0` disables |
| `MCP_AWS_EXPAND_CONCURRENCY` | `8` | Parallel detail calls during `expand` |
| `MCP_AWS_LOG_LEVEL` | `INFO` | Logging level (stderr only) |

## Coverage today

`account` (identity, regions, tagged-resource sweep), `org` (accounts, roots, OUs),
`ec2` (instances, security groups, VPCs, subnets, route tables, volumes, NAT gateways),
`elbv2` (load balancers, target groups, listeners, target health), `autoscaling`
(groups), `eks` (clusters, node groups, add-ons, Fargate profiles).

## Development

```bash
uv run pytest          # no network, no credentials; AWS is stubbed
```
