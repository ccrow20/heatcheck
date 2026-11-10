PY ?= python3
CDK ?= npx --yes aws-cdk@2
BACKEND = cd backend && PYTHONPATH=src

# Stack name and region come from infra/config.json (defaults: heatcheck, us-east-1).
STACK ?= $(shell $(PY) -c "import json,pathlib;p=pathlib.Path('infra/config.json');print(json.loads(p.read_text()).get('project','heatcheck') if p.exists() else 'heatcheck')")
REGION ?= $(shell $(PY) -c "import json,pathlib;p=pathlib.Path('infra/config.json');print(json.loads(p.read_text()).get('aws_region','us-east-1') if p.exists() else 'us-east-1')")

# Outputs of the deployed stack, read with the AWS CLI only by the targets that use
# them. The `X ?= $(eval X := ...)$(X)` form caches the first lookup, so each one
# is fetched at most once per run. Set one yourself to skip it: make publish DATA_BUCKET=...
stack_output = $(shell aws cloudformation describe-stacks --region $(REGION) --stack-name $(STACK) \
  --query "Stacks[0].Outputs[?OutputKey=='$(1)'].OutputValue" --output text)
DATA_BUCKET ?= $(eval DATA_BUCKET := $(call stack_output,DataBucketName))$(DATA_BUCKET)
SITE_BUCKET ?= $(eval SITE_BUCKET := $(call stack_output,SiteBucketName))$(SITE_BUCKET)
DISTRIBUTION_ID ?= $(eval DISTRIBUTION_ID := $(call stack_output,DistributionId))$(DISTRIBUTION_ID)
FUNCTION_NAME ?= $(eval FUNCTION_NAME := $(call stack_output,FunctionName))$(FUNCTION_NAME)
GITHUB_ROLE_ARN ?= $(eval GITHUB_ROLE_ARN := $(call stack_output,GithubDeployRoleArn))$(GITHUB_ROLE_ARN)

# $(call require,VAR) stops with a clear message when a stack output came back empty.
require = @if [ -z "$($(1))" ] || [ "$($(1))" = "None" ]; then \
  echo "Couldn't read $(1) from stack $(STACK) in $(REGION). Deploy first (make deploy) and check your AWS credentials."; \
  exit 1; fi

.PHONY: help install infra-install test infra-test lint format sample serve run-local bootstrap diff deploy \
        destroy invoke backfill publish set-api-key github-vars check-sources deploy-site clean

help: ## Show this list
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

# local development
install: ## Install the backend dev tools (pytest, ruff, boto3)
	cd backend && $(PY) -m pip install -r requirements-dev.txt

infra-install: ## Install the CDK libraries (needs Node.js too)
	cd infra && $(PY) -m pip install -r requirements.txt -r requirements-dev.txt

test: ## Run the backend unit tests
	cd backend && $(PY) -m pytest

infra-test: ## Run the infrastructure tests (synthesizes the stack and checks its security settings)
	cd infra && $(PY) -m pytest

lint: ## Lint and format-check all Python
	cd backend && ruff check . && ruff format --check .
	cd infra && ruff check . && ruff format --check .

format: ## Auto-format all Python
	cd backend && ruff format . && ruff check --fix .
	cd infra && ruff format . && ruff check --fix .

sample: ## Build fictional sample data into frontend/data for local preview
	$(BACKEND) $(PY) scripts/make_sample_data.py

serve: ## Serve the website at http://localhost:8000
	cd frontend && $(PY) -m http.server 8000

run-local: ## Do what the Lambda does, against local folders (real data)
	$(BACKEND) $(PY) -m heatcheck.cli run

check-sources: ## Check the live APIs still parse (set APINBA_KEY to include API-NBA)
	$(BACKEND) $(PY) scripts/check_sources.py --source espn
	@if [ -n "$$APINBA_KEY" ]; then $(BACKEND) $(PY) scripts/check_sources.py --source apinba; fi

# AWS
bootstrap: ## One time per account and region: create CDK's deployment resources
	cd infra && $(CDK) bootstrap

diff: ## Show what `make deploy` would change
	cd infra && $(CDK) diff

deploy: ## Create or update everything in AWS (infrastructure, Lambda code, website)
	cd infra && $(CDK) deploy

destroy: ## Delete the stack (the two buckets are kept so your data survives; delete them by hand)
	cd infra && $(CDK) destroy

# The read timeout outlasts the Lambda's longest run: if the CLI timed out first,
# it would retry the call and run the pipeline a second time.
invoke: ## Run the deployed pipeline now
	$(call require,FUNCTION_NAME)
	aws lambda invoke --region $(REGION) --function-name $(FUNCTION_NAME) --cli-read-timeout 900 \
	  --cli-binary-format raw-in-base64-out --payload '{}' /dev/stdout

backfill: ## Load a date range into S3: make backfill START=2025-10-21 END=2026-11-05
	$(if $(and $(START),$(END)),,$(error Set START and END, e.g. make backfill START=2025-10-21 END=2026-11-05))
	$(call require,DATA_BUCKET)
	$(BACKEND) AWS_DEFAULT_REGION=$(REGION) $(PY) -m heatcheck.cli ingest --start $(START) --end $(END) \
	  --data s3://$(DATA_BUCKET) --skip-existing

publish: ## Recompute streaks.json from everything stored in S3
	$(call require,DATA_BUCKET)
	$(call require,SITE_BUCKET)
	$(BACKEND) AWS_DEFAULT_REGION=$(REGION) $(PY) -m heatcheck.cli publish --data s3://$(DATA_BUCKET) --site s3://$(SITE_BUCKET)

set-api-key: ## Store the optional API-NBA key in SSM (hidden prompt, nothing in shell history)
	$(BACKEND) $(PY) -m heatcheck.cli set-api-key --name /$(STACK)/apinba-api-key --region $(REGION)

github-vars: ## Point GitHub Actions at the deployed stack (needs the gh CLI; run inside the repo)
	$(call require,SITE_BUCKET)
	@if [ -z "$(GITHUB_ROLE_ARN)" ] || [ "$(GITHUB_ROLE_ARN)" = "None" ]; then \
	  echo "The stack has no GitHub deploy role: set github_repo in infra/config.json and run make deploy"; exit 1; fi
	gh variable set AWS_REGION --body "$(REGION)"
	gh variable set AWS_DEPLOY_ROLE_ARN --body "$(GITHUB_ROLE_ARN)"
	gh variable set SITE_BUCKET --body "$(SITE_BUCKET)"
	gh variable set CLOUDFRONT_DISTRIBUTION_ID --body "$(DISTRIBUTION_ID)"
	gh variable set LAMBDA_FUNCTION_NAME --body "$(FUNCTION_NAME)"
	@echo "Done. Every push to main now deploys the site and the Lambda code."

deploy-site: ## Upload just the website by hand (make deploy and CI also do this)
	$(call require,SITE_BUCKET)
	$(call require,DISTRIBUTION_ID)
	aws s3 sync frontend/ s3://$(SITE_BUCKET) --delete --exclude "data/*" --exclude "*.html" --cache-control "public, max-age=3600"
	aws s3 sync frontend/ s3://$(SITE_BUCKET) --delete --exclude "*" --include "*.html" --cache-control "no-cache"
	aws cloudfront create-invalidation --distribution-id $(DISTRIBUTION_ID) --paths "/*" \
	  --query 'Invalidation.Id' --output text

clean: ## Remove caches and build output
	rm -rf backend/.pytest_cache backend/.ruff_cache infra/.pytest_cache infra/.ruff_cache infra/cdk.out .localdata
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
