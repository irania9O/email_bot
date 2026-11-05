# Build the image locally and ship it to the server over SSH
# (the server can't reach PyPI/Docker Hub, so it can't build itself).
#
#   make deploy          build, copy, restart, show logs
#   make logs / status / restart / ssh
#   make deploy HOST=nl  use another SSH host

HOST    ?= iran
DIR     ?= /opt/email_bot
IMAGE   ?= iust-mail:latest
HUB     ?= irania9o/iust-mail:latest

SHELL := /bin/bash
.SHELLFLAGS := -eo pipefail -c
.PHONY: build push up deploy logs status restart ssh check hub run

build:
	docker compose build

# Copy the image and compose file to the server
push:
	ssh $(HOST) 'mkdir -p $(DIR)/data && chown 1000:1000 $(DIR)/data'
	docker save $(IMAGE) | gzip | ssh $(HOST) 'gunzip | docker load'
	scp docker-compose.yml $(HOST):$(DIR)/
	@ssh $(HOST) 'test -f $(DIR)/.env' || { echo "!! $(DIR)/.env missing on $(HOST); copy it first: scp .env $(HOST):$(DIR)/.env"; exit 1; }

up:
	ssh $(HOST) 'cd $(DIR) && docker compose up -d --no-build --force-recreate'

# Confirm the server runs the code we just built. Image IDs differ between Docker image
# stores (the server uses containerd), so compare a hash of the source files instead.
SRC_HASH = cd /app/src && find . -name "*.py" | sort | xargs sha256sum | sha256sum | cut -c1-12
check:
	@local=$$(docker run --rm --entrypoint sh $(IMAGE) -c '$(SRC_HASH)'); \
	remote=$$(ssh $(HOST) "docker exec iust-mail sh -c '$(SRC_HASH)'"); \
	if [ "$$local" = "$$remote" ]; then echo "OK: $(HOST) runs code $$local"; \
	else echo "MISMATCH: local code $$local vs $(HOST) $$remote"; exit 1; fi

deploy: build push up check
	@sleep 10
	ssh $(HOST) 'cd $(DIR) && docker compose logs --tail 5'

# Push the image to Docker Hub (needs `docker login`; the repo is public unless you make it private)
hub: build
	docker tag $(IMAGE) $(HUB)
	docker push $(HUB)

# Run locally in the foreground (stop the server bot first, or both will answer)
run: build
	docker compose up

logs:
	ssh $(HOST) 'cd $(DIR) && docker compose logs -f --tail 50'

status:
	ssh $(HOST) 'cd $(DIR) && docker compose ps'

restart:
	ssh $(HOST) 'cd $(DIR) && docker compose restart'

ssh:
	ssh -t $(HOST) 'cd $(DIR) && exec $$SHELL -l'
