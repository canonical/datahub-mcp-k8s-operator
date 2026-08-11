# Contributing

To make contributions to this charm, you'll need a working [development setup](https://juju.is/docs/sdk/dev-setup).

A lot of the commands you would need are covered with the [Makefile](./Makefile), learn more by running `make help`.

**Note:** It is recommended to build on the host and deploy in a [Multipass](https://canonical.com/multipass/install) instance.
Use `multipass mount` to mount the project directory with the build artifacts into your Multipass instance.

## Environment for coding

You can install the dependencies for coding with:

```shell
# uv
sudo snap install astral-uv --channel latest/stable --classic
uv version
#> uv 0.9.5 (d5f39331a 2025-10-21)

# Tox
# Note: If you do this from the VSCode snap's integrated terminal
# you will have a weird PATH. So, do it from an external terminal.
uv tool install tox --with tox-uv
tox --version
#> 4.32.0
```

You can create an environment for coding with:

```shell
make venv
source venv/bin/activate
```

## Environment for building

You can install the dependencies for building with:

```shell
# LXD
sudo snap install lxd --channel 5.21/stable
lxd version
#> 5.21.4 LTS

sudo adduser $USER lxd
newgrp lxd
lxd init --auto

# Charmcraft
sudo snap install charmcraft --channel latest/stable --classic
charmcraft version
#> charmcraft 4.0.1

# Rockcraft
sudo snap install rockcraft --channel latest/stable --classic
rockcraft version
#> rockcraft 1.16.0

# yq
sudo snap install yq
yq --version
#> yq (https://github.com/mikefarah/yq/) version v4.49.2

# Required by `import_rock.sh`
sudo snap alias rockcraft.skopeo skopeo
```

### Verify build environment

You can verify that you have all the necessary dependencies installed with:

```shell
make check-build-deps
```

## Building artifacts

You can build the charm with:

```shell
make build-charm
```

You can build the rock with:

```shell
make build-rock
```

**Common issues:** Some frequent issues with the build step and how to solve them are listed below.
1. *Missing repository*: Sometimes `multipass` forgets mounts. The solution is to unmount and mount again, from the host machine:
```shell
multipass unmount charm-dev
multipass mount /path/to/repository charm-dev:/path/to/repository
```

2. *Permission error in pack*: `tox` commands create directories inside the repository that can cause permission issues. The solution is to remove those directories before running `charmcraft pack`.
```shell
cd /path/to/repository
rm -rf .tox venv
```

3. *Nothing to be done for rock*: The rock building target watches for changes to `rockcraft.yaml` and `files/serve.py` for deciding if it needs to rebuild. If you need to change something else, `touch rockcraft.yaml` to force a build.

## Pinning the upstream version

The rock installs `mcp-server-datahub` from PyPI, pinned to the `version` field at the top of [`rock/rockcraft.yaml`](rock/rockcraft.yaml). To move to a new upstream release, change that one field and rebuild. `${CRAFT_PROJECT_VERSION}` is injected by `rockcraft` and is what the `pip install` in `override-build` resolves.

## Code quality

You can run linters, static analysis, and unit tests with:

```shell
make fmt     # Runs formatters
make lint    # Runs linters
make test    # Runs static analysis, unit tests and rock entrypoint tests
make checks  # Runs all of the above

make test-serve        # Runs the rock entrypoint tests
make test-integration  # Runs integration tests
```

*: It is recommended to let CI runners run integration tests on GitHub Actions.

### What each suite covers

None of them need a DataHub deployment.

| Suite | Needs | Covers |
|---|---|---|
| `test-unit` | nothing | Charm logic: relation and secret handling, status, the pebble layer it plans |
| `test-serve` | nothing | The real workload, booted against a stub GMS: routes, tool set, and client authentication |
| `test-integration` | a Juju controller | The charm packs and deploys, the rock is pullable and its entrypoint imports, refresh and scaling |

The integration suite deploys the charm **without** a `datahub-client` provider and asserts it
settles into its blocked state. Everything past that point is covered by the other two suites,
which is why nothing here requires standing up DataHub and its machine dependencies.

## Charm libraries owned by other repositories

[`lib/charms/datahub_k8s/v0/datahub_client.py`](lib/charms/datahub_k8s/v0/datahub_client.py) defines both sides of the `datahub_client` interface and is **owned by `datahub-k8s-operator`**. Edit it there, bump `LIBPATCH` and publish, then fetch the new version here.

## Deploying locally

### Multipass environment setup

The recommended deployment environment is set up on `multipass`.

Get it via:

```shell
sudo snap install multipass --classic
```

Steps to setting up the environment:
```shell
multipass launch -c 4 -m 16G -d 50G -n charm-dev charm-dev
multipass shell charm-dev
# Refer to [1] below for a note.
juju switch microk8s
juju add-model datahub-k8s
# Refer to [2] below for a note.
```

[1]: Minor and/or patch versions of the tech stack can introduce regressions, if you are having problems use the following commands to switch to tested channels:
```shell
sudo snap switch juju --channel 3/stable
sudo snap refresh juju
sudo snap switch microk8s --channel 1.34-strict/stable
sudo snap refresh microk8s
```

Afterwards, you will have to recreate the controller and model from scratch:
```shell
juju bootstrap microk8s microk8s
juju add-model datahub-k8s
```

[2]: For a better development experience, you can turn on debug logging in a model with the following:
```shell
juju switch controller:model
juju model-config logging-config="<root>=INFO;unit=DEBUG"
```

### Deployment environment dependencies

Install the following inside your `multipass` instance:

```shell
# Juju
sudo snap install juju --channel 3/stable
juju version
#> 3.6.12-genericlinux-amd64

# MicroK8s
sudo snap install microk8s --channel 1.34-strict/stable
microk8s version
#> MicroK8s v1.34.1 revision 8447

sudo microk8s enable hostpath-storage
sudo microk8s enable registry

sudo usermod -aG snap_microk8s $USER

# Docker
sudo snap install docker --channel latest/stable
docker version
#> ... 28.4.0 ...

sudo groupadd docker
sudo usermod -aG docker $USER
newgrp docker

sudo snap disable docker
sudo snap enable docker

# Both microk8s and docker require new groups
# and `newgrp` does not cover for both at the same time.
# A system reboot is recommended at this point.

juju bootstrap microk8s
```

You can verify that all deployment dependencies are installed with:

```shell
make check-deploy-deps
```

### Deploy the dependencies

This charm's only dependency is DataHub itself, which must be active in the target model for the charm to leave its blocked state. Follow the `datahub-k8s-operator` CONTRIBUTING guide to deploy it, then come back here.

You do not need it to run any of the test suites, check [What each suite covers](#what-each-suite-covers).

### Deploy the charm

First, build and import the rock into MicroK8s (run on the host where you built):

```shell
make import-rock
```

Then deploy and relate it:

```shell
juju switch microk8s:datahub-k8s

make build-charm
make deploy-local

juju integrate datahub-mcp-k8s datahub-k8s
```

**Note:** It is highly recommended to wait between commands to let `juju status` show an `active-idle` status for the charm.

The charm blocks until the `datahub-client` relation is in place; that relation is what provisions its DataHub service account and access token, so nothing else is needed to get it serving.
