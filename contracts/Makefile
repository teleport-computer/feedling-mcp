# Feedling contracts — convenience targets.
#
# Usage:
#   make test                    run forge tests
#   make deploy CHAIN=eth_sepolia            deploy FeedlingAppAuth to $CHAIN
#   make deploy CHAIN=base_sepolia
#   make add-hash CHAIN=eth_sepolia COMPOSE_HASH=0x... COMMIT=abc123 YAML_URL=https://...
#
# Env loaded from contracts/.env (gitignored). Required keys:
#   PRIVATE_KEY, FEEDLING_APP_AUTH_OWNER (deploy only),
#   BASESCAN_API_KEY (verify only), and per-chain RPC URLs.

-include .env
export

# Per-chain aliases — extend as we add chains.
ifeq ($(CHAIN),eth_sepolia)
  RPC_URL          ?= $(ETH_SEPOLIA_RPC_URL)
  CHAIN_ID         := 11155111
  VERIFIER_URL     := https://api.etherscan.io/v2/api?chainid=11155111
  API_KEY_VAR      := ETHERSCAN_API_KEY
else ifeq ($(CHAIN),base_sepolia)
  RPC_URL          ?= $(BASE_SEPOLIA_RPC_URL)
  CHAIN_ID         := 84532
  VERIFIER_URL     := https://api.etherscan.io/v2/api?chainid=84532
  API_KEY_VAR      := BASESCAN_API_KEY
else ifeq ($(CHAIN),base)
  RPC_URL          ?= $(BASE_RPC_URL)
  CHAIN_ID         := 8453
  VERIFIER_URL     := https://api.etherscan.io/v2/api?chainid=8453
  API_KEY_VAR      := BASESCAN_API_KEY
endif

test:
	forge test -vvv

build:
	forge build --sizes

coverage:
	forge coverage --report summary

# -----------------------------------------------------------------------------
# Deploy FeedlingAppAuth (once per chain per lifetime).
# -----------------------------------------------------------------------------
deploy: require-chain
	@echo ">>> Deploying FeedlingAppAuth to $(CHAIN) (id $(CHAIN_ID))"
	forge script script/DeployFeedlingAppAuth.s.sol \
		--rpc-url $(RPC_URL) \
		--private-key $(PRIVATE_KEY) \
		--broadcast \
		--verify \
		--verifier etherscan \
		--verifier-url "$(VERIFIER_URL)" \
		--etherscan-api-key "$($(API_KEY_VAR))" \
		-vvv

# If verification is flaky (e.g. DNS intercept), drop --verify:
deploy-noverify: require-chain
	@echo ">>> Deploying FeedlingAppAuth to $(CHAIN) without source verification"
	forge script script/DeployFeedlingAppAuth.s.sol \
		--rpc-url $(RPC_URL) \
		--private-key $(PRIVATE_KEY) \
		--broadcast \
		-vvv

# -----------------------------------------------------------------------------
# Publish a new compose_hash authorization.
# Use deploy/publish-compose-hash.sh for the convenient wrapper that hashes
# the compose file for you.
# -----------------------------------------------------------------------------
add-hash: require-chain
ifndef COMPOSE_HASH
	$(error COMPOSE_HASH not set. Run from deploy/publish-compose-hash.sh or set it manually.)
endif
ifndef COMMIT
	$(error COMMIT not set)
endif
ifndef YAML_URL
	$(error YAML_URL not set)
endif
	@FEEDLING_PUBLISH_CHAIN="$(CHAIN)" \
	FEEDLING_PUBLISH_RPC_URL="$(RPC_URL)" \
	FEEDLING_COMPOSE_HASH="$(COMPOSE_HASH)" \
	FEEDLING_GIT_COMMIT="$(COMMIT)" \
	FEEDLING_COMPOSE_YAML_URL="$(YAML_URL)" \
	FEEDLING_PUBLISH_EVIDENCE_DIR="$${FEEDLING_PUBLISH_EVIDENCE_DIR:-$$(git rev-parse --path-format=absolute --git-path compose-publish-evidence)}" \
	python3 ../deploy/compose_publisher.py

# -----------------------------------------------------------------------------
# Read-only helpers
# -----------------------------------------------------------------------------
check-hash: require-chain
ifndef COMPOSE_HASH
	$(error COMPOSE_HASH not set)
endif
	@cast call $(FEEDLING_APP_AUTH_CONTRACT) "isAppAllowed(bytes32)(bool)" $(COMPOSE_HASH) --rpc-url $(RPC_URL)

list-releases: require-chain
	@count=$$(cast call $(FEEDLING_APP_AUTH_CONTRACT) "releaseCount()(uint256)" --rpc-url $(RPC_URL)); \
	echo "Total releases: $$count"; \
	for i in $$(seq 0 $$(($$count - 1))); do \
		echo "--- [$$i] ---"; \
		cast call $(FEEDLING_APP_AUTH_CONTRACT) "getRelease(uint256)(bytes32,bool,uint64,uint64,string,string)" $$i --rpc-url $(RPC_URL); \
	done

# -----------------------------------------------------------------------------
# Guards
# -----------------------------------------------------------------------------
require-chain:
ifndef CHAIN
	$(error CHAIN not set — valid values: eth_sepolia, base_sepolia, base)
endif
ifndef RPC_URL
	$(error No RPC_URL for CHAIN=$(CHAIN). Set $(CHAIN)_RPC_URL in .env or pass RPC_URL directly.)
endif

.PHONY: test build coverage deploy deploy-noverify add-hash check-hash list-releases require-chain
