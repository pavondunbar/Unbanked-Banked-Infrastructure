.PHONY: help up down build restart logs ps health demo clean nuke \
       test test-unit test-e2e \
       db-balances db-ledger db-transfers db-settlements db-fx \
       topics kafka-tail shell-pg shell-kafka \
       seed-accounts integrity

DC := docker compose
PG := $(DC) exec postgres psql -U unbanked
KAFKA_BIN := /opt/kafka/bin

# ── Help ──────────────────────────────────────────────────

help: ## Show this help
	@echo "\033[1m\033[94mUNBANKED — Cross-Border Stablecoin Infrastructure\033[0m\n"
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ── Lifecycle ─────────────────────────────────────────────

up: ## Start all services (build + wait for healthy)
	$(DC) up -d --build
	@echo "\n\033[32mStack is up. API Gateway: http://localhost:8000\033[0m"
	@echo "Run \033[1mmake demo\033[0m to exercise all corridors."

down: ## Stop all services (preserves data)
	$(DC) down

build: ## Build all Docker images
	$(DC) build

restart: ## Restart all services
	$(DC) restart

logs: ## Tail all service logs
	$(DC) logs -f --tail=50

ps: ## Show running containers
	$(DC) ps

clean: ## Stop and remove containers + volumes
	$(DC) down -v --remove-orphans

nuke: ## Full reset — containers, volumes, images, orphans
	$(DC) down -v --remove-orphans --rmi local
	@echo "\033[33mNuked. Run 'make up' to rebuild from scratch.\033[0m"

# ── Health & Observability ────────────────────────────────

health: ## Check health of all services via API gateway
	@curl -sf http://localhost:8000/health \
		-H "X-API-Key: admin-key-001" | python3 -m json.tool \
		|| echo "\033[31mGateway unreachable. Is the stack running?\033[0m"

integrity: ## Verify ledger integrity (debits == credits, no negatives)
	@echo "\033[1mLedger Integrity Check\033[0m"
	@$(PG) -c "\
		SELECT \
			COUNT(*) AS entries, \
			SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) AS total_debits, \
			SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) AS total_credits, \
			CASE WHEN ABS( \
				SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) - \
				SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) \
			) < 0.01 THEN 'BALANCED' ELSE 'MISMATCH' END AS status \
		FROM journal_entries;"

# ── Kafka ─────────────────────────────────────────────────

topics: ## List all Kafka topics
	$(DC) exec kafka $(KAFKA_BIN)/kafka-topics.sh \
		--bootstrap-server localhost:9092 --list

kafka-tail: ## Tail settled transfer events
	$(DC) exec kafka $(KAFKA_BIN)/kafka-console-consumer.sh \
		--bootstrap-server localhost:9092 \
		--topic transfer.settled --from-beginning --max-messages 10

# ── Database Inspection ───────────────────────────────────

db-balances: ## Show all wallet balances (derived from journal)
	@$(PG) -c "\
		SELECT wallet_id, currency, \
			SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) - \
			SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) AS balance \
		FROM journal_entries GROUP BY wallet_id, currency \
		HAVING SUM(CASE WHEN direction='credit' THEN amount ELSE 0 END) - \
			SUM(CASE WHEN direction='debit' THEN amount ELSE 0 END) <> 0 \
		ORDER BY wallet_id;"

db-ledger: ## Show recent journal entries
	@$(PG) -c "\
		SELECT id, entry_ref, wallet_id, direction, amount, currency, \
			coa_code, created_at \
		FROM journal_entries ORDER BY id DESC LIMIT 20;"

db-transfers: ## Show recent transfers
	@$(PG) -c "\
		SELECT id, corridor, send_amount, send_currency, \
			receive_amount, receive_currency, status, created_at \
		FROM transfers ORDER BY created_at DESC LIMIT 20;"

db-settlements: ## Show settlements with state machine status
	@$(PG) -c "\
		SELECT s.id, s.status, s.priority, s.approved_by, s.signed_by, \
			s.blockchain_tx_hash, t.send_amount, t.send_currency \
		FROM settlements s JOIN transfers t ON s.transfer_id = t.id \
		ORDER BY s.created_at DESC LIMIT 20;"

db-fx: ## Show active FX rates
	@$(PG) -c "\
		SELECT base_currency, quote_currency, mid_rate, spread_bps \
		FROM fx_rates WHERE active = true \
		ORDER BY base_currency, quote_currency;"

# ── Shells ────────────────────────────────────────────────

shell-pg: ## Open interactive psql shell
	$(PG)

shell-kafka: ## Open shell inside Kafka container
	$(DC) exec kafka /bin/bash

# ── Seeding ───────────────────────────────────────────────

seed-accounts: ## Seed demo clients, wallets, and agents via API
	@echo "Seeding accounts — run 'make demo' for the full walkthrough."
	@python3 -c "\
	import httpx; \
	H={'X-API-Key':'admin-key-001'}; \
	B='http://localhost:8000'; \
	c=httpx.post(f'{B}/clients',headers=H,json={'full_name':'Demo User','client_type':'unbanked','phone':'+1-555-000-0001','country_code':'US'}).json(); \
	print(f\"Client: {c.get('id','err')}\"); \
	httpx.post(f'{B}/kyc/verify',headers=H,json={'client_id':c['id'],'tier':'basic'}); \
	w=httpx.post(f'{B}/wallets',headers=H,json={'client_id':c['id'],'currency':'USD'}).json(); \
	print(f\"Wallet: {w.get('id','err')}\"); \
	httpx.post(f'{B}/wallets/fund',headers=H,json={'wallet_id':w['id'],'amount':1000,'currency':'USD','method':'bank_deposit','bank_ref':'SEED-001'}); \
	print('Funded 1000 USD. Done.')"

# ── Demo & Tests ──────────────────────────────────────────

demo: ## Run end-to-end demo (requires running stack)
	python3 scripts/demo.py

test: ## Run all tests (unit — no Docker needed)
	python3 -m pytest tests/ -v

test-unit: ## Run unit tests only
	python3 -m pytest tests/test_core.py -v

test-e2e: ## Run e2e tests (requires running stack)
	python3 -m pytest tests/ -v -k e2e
