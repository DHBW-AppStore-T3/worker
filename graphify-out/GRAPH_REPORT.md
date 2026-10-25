# Graph Report - worker  (2026-09-18)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 1238 nodes · 2149 edges · 80 communities (76 shown, 4 thin omitted)
- Extraction: 96% EXTRACTED · 4% INFERRED · 0% AMBIGUOUS · INFERRED: 89 edges (avg confidence: 0.92)
- Token cost: 87,334 input · 901 output

## Graph Freshness
- Built from commit: `f82d1887`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Test Fixtures and Mocks
- Packer Template Discovery Tests
- Packer Executor Init/Build
- Deployment Task Lifecycle
- Structured Logger Utilities
- Packer Template Discovery
- Celery App Configuration
- Logger Utility Helpers
- Structured Logger Core
- OpenStack Credential Context
- Terraform Vars Roster
- Scalar Value Coercion
- OpenStack Service
- Subprocess Streaming
- Terraform Executor Tests
- Clouds YAML Construction
- Packer Validate Command
- Fernet Encrypt Decrypt
- Packer Error Extraction
- Terraform Executor
- Logger Output Formatting
- LogEntry Serialization
- Logger Export and Inspection
- Redis Build Lock
- Postgres Backend Override
- Failure Exception Serialization
- Crypto and Auth Modules
- Lock Acquire Polling
- OpenStack CLI Runner
- Image Existence Check
- Structured Logger Level Methods
- Logger Progress Events
- Logger Operation Timing
- Git Service
- Credential Envelope Error
- Packer Vars Encoding
- Terraform Compute Instance Extraction
- Test Config and Fixtures
- Cipher Key Validation
- Clouds YAML Shredding
- Terraform Vars Encoding
- Terraform Env Merging
- Packer Executor Methods
- Terraform Output Collection
- Text Truncation
- OpenStack Server Show
- Settings Configuration Tests
- Build Lock Release
- Git URL Parsing
- Logger Entry Construction
- Password Auth Flow
- OpenStack Server Stop
- OpenStack Server Start
- Terraform Plan Command
- Terraform Destroy Command
- Terraform Output Parsing
- Decrypt B64 Error Paths
- Git Clone Operations
- Build Lock Context Manager
- Console Log Sink
- Clouds YAML File Recovery
- Build Lock Test Fixtures
- Redis Build Lock Service
- Nested None Scrubbing
- Base64 Encrypt Decrypt
- Heartbeat Loop Tests
- Git Auth URL Generation
- Terraform State Pull
- Crypto Module Reload
- Logger Factory Tests
- OpenStack Image List
- Git Integration Tests
- Git Service Test Fixtures
- Git Cleanup Tests
- Logger Emitter Error Handling
- Task Import Smoke Tests
- Subprocess Stdout Stub
- Lock Acquire Heartbeat
- Test Package Init
- Worker Root Package

## God Nodes (most connected - your core abstractions)
1. `TerraformExecutor` - 56 edges
2. `PerTaskCloudsConfig` - 48 edges
3. `StructuredLogger` - 35 edges
4. `deploy_application()` - 34 edges
5. `_discover_packer_templates()` - 33 edges
6. `_make_envelope()` - 32 edges
7. `PackerBuildLock` - 30 edges
8. `LogCategory` - 26 edges
9. `redeploy_resource()` - 26 edges
10. `destroy_deployment()` - 25 edges

## Surprising Connections (you probably didn't know these)
- `TestDeployApplication` --uses--> `Failure`  [INFERRED]
  tests/test_tasks_branches.py → app/tasks.py
- `TestDestroyDeployment` --uses--> `Failure`  [INFERRED]
  tests/test_tasks_branches.py → app/tasks.py
- `TestPauseResume` --uses--> `Failure`  [INFERRED]
  tests/test_tasks_branches.py → app/tasks.py
- `TestRedeployResource` --uses--> `Failure`  [INFERRED]
  tests/test_tasks_branches.py → app/tasks.py
- `TestBadKeys` --uses--> `PackerTemplateDiscoveryError`  [INFERRED]
  tests/test_packer_discovery.py → app/services/packer_discovery.py

## Import Cycles
- None detected.

## Communities (80 total, 4 thin omitted)

### Community 0 - "Test Fixtures and Mocks"
Cohesion: 0.06
Nodes (57): _FakeTemplate, _make_build_lock_mock(), _make_clouds_config_mock(), _make_git_mock(), _make_openstack_service_mock(), _make_packer_mock(), _make_terraform_executor_mock(), _patch_git_repo() (+49 more)

### Community 1 - "Packer Template Discovery Tests"
Cohesion: 0.05
Nodes (32): Path, _make_legacy_layout(), _make_subdir_template(), A helper subdir without ``template.pkr.hcl`` does not turn it into multi-mode., Multi-template subdirectory layout (``packer/<key>/template.pkr.hcl``)., Two valid subdirs each with ``template.pkr.hcl`` produce two templates., Templates are returned sorted alphabetically by key., Each returned template has paths under its subdirectory. (+24 more)

### Community 2 - "Packer Executor Init/Build"
Cohesion: 0.06
Nodes (25): Exception, unit, init exports PACKER_LOG=1 in the subprocess environment., init forwards the executor's output_callback to _stream_subprocess., init catches exceptions and returns (False, '', str(e))., Callable recorder replacing ``_stream_subprocess``. Captures invocation kwargs…, Behaviour of ``PackerExecutor.build``., build with force=True adds the -force flag. (+17 more)

### Community 3 - "Deployment Task Lifecycle"
Cohesion: 0.10
Nodes (30): _apply_image_name_vars(), _cleanup_task_resources(), deploy_application(), _emit(), _stream_line(), destroy_deployment(), encode_terraform_vars(), _extract_commit_info() (+22 more)

### Community 4 - "Structured Logger Utilities"
Cohesion: 0.06
Nodes (26): app_utils, fixture, unit, Tests for app.utils.logger. The instructions mention helpers named…, _now_iso returns an ISO-8601 string ending in 'Z'., _now_iso contains 'T' separating date and time., A StructuredLogger with the console sink disabled., phase() emits an entry whose message is the phase name in uppercase. (+18 more)

### Community 5 - "Packer Template Discovery"
Cohesion: 0.09
Nodes (25): Git service for repository management with HTTPS token authentication., _discover_packer_templates(), _PackerTemplate, PackerTemplateDiscoveryError, Discover Packer templates inside a cloned app repository. Two layouts are…, A single Packer template discovered under ``packer/``. Attributes: key: Stable…, Raised when the ``packer/`` layout can't be interpreted unambiguously., Walk ``<repo_path>/packer/`` and return the templates it contains. See the… (+17 more)

### Community 6 - "Celery App Configuration"
Cohesion: 0.09
Nodes (22): Settings, PackerExecutor, OutputCallback, Packer execution utilities with comprehensive structured logging. ``init`` and…, Executor for Packer operations with detailed logging. ``output_callback`` is…, Terraform execution utilities with comprehensive structured logging. Each long-…, BaseSettings, celery (+14 more)

### Community 7 - "Logger Utility Helpers"
Cohesion: 0.08
Nodes (19): Utility functions and helpers for the worker app., _Buffer, _ConsoleSink, LogEntry, LogLevel, Structured logging for deployment tasks. Three concerns are kept separate here:…, A single log entry with explicit slots for common metadata. Putting…, Flat JSON-serialisable representation; ``None`` fields are skipped. (+11 more)

### Community 8 - "Structured Logger Core"
Cohesion: 0.19
Nodes (8): LogCategory, Any, Exception, Buffer + console + optional event-bus emitter for one deployment task., Mark a major deployment phase. Live progress is emitted separately via…, Buffer a captured command output block (post-completion). For per-line…, Log an exception with its real traceback. Uses ``format_exception(type, exc,…, StructuredLogger

### Community 9 - "OpenStack Credential Context"
Cohesion: 0.11
Nodes (15): PerTaskCloudsConfig, Context manager: materialize a per-task `clouds.yaml`, then shred it. Usage:…, The decrypt-failure message should hint at the shared CREDENTIAL_ENCRYPTION_KEY., Validation of the dispatched envelope dict., A None envelope must raise CredentialEnvelopeError, not TypeError., A non-dict envelope (e.g. a list) must raise CredentialEnvelopeError., A string envelope must raise CredentialEnvelopeError., Envelope without encrypted_identifier_b64 must raise with the field name. (+7 more)

### Community 10 - "Terraform Vars Roster"
Cohesion: 0.11
Nodes (18): _build_current_roster(), _looks_like_file_var_value(), True if ``value`` matches the file-upload shape produced by the backend's…, Return a copy of ``terraform_vars`` with file-shape entries removed. Pure…, Compute the legal slot-key sets for the current roster. Returns a ``(team_keys,…, Drop scoped-map entries whose slot keys no longer match the roster. Redeploy…, _reconcile_scoped_vars_to_roster(), _strip_file_vars() (+10 more)

### Community 11 - "Scalar Value Coercion"
Cohesion: 0.11
Nodes (12): Coerce one context value into a JSON-friendly scalar. Tools like ``orjson``…, _scalar_or_str(), _scalar_or_str returns strings unchanged., _scalar_or_str returns ints unchanged., _scalar_or_str returns floats unchanged., _scalar_or_str returns bools unchanged., _scalar_or_str returns None unchanged., _scalar_or_str recurses into dicts, converting non-scalar leaves. (+4 more)

### Community 12 - "OpenStack Service"
Cohesion: 0.11
Nodes (15): OpenStackService, OpenStack service for image management, Return the parsed ``openstack server show <id> -f json`` payload. Used to log…, Stop an OpenStack compute instance. Returns ``(True, None)`` on success,…, Service for OpenStack image and compute-instance operations. All methods shell…, Start an OpenStack compute instance. Returns ``(True, None)`` on success,…, Initialize OpenStack service Args: env_vars: OpenStack environment variables…, Run an ``openstack`` CLI command with the configured env vars. Centralised so… (+7 more)

### Community 13 - "Subprocess Streaming"
Cohesion: 0.13
Nodes (13): OutputCallback, Run a subprocess and stream its output line-by-line. stdout and stderr are…, _stream_subprocess(), FakePopen, When output_callback is None, lines still accumulate into stdout., A raising callback never aborts draining; subsequent lines still arrive., A Popen.wait timeout triggers os.killpg and returns (124, partial, "Timeout")., killpg raising OSError on timeout is swallowed; we still return the timeout… (+5 more)

### Community 14 - "Terraform Executor Tests"
Cohesion: 0.11
Nodes (14): _patch_stream(), Patch the module-level _stream_subprocess used by TerraformExecutor., Verify terraform init command shape and success/failure handling., When no schema is configured, -reconfigure is not added and no override file is…, With backend_schema_name, init adds -reconfigure and writes the override file., Non-zero return from the stream surfaces as success=False., An exception inside the init body returns (False, "", str(e))., Verify terraform apply command shape including targets and replaces. (+6 more)

### Community 15 - "Clouds YAML Construction"
Cohesion: 0.13
Nodes (13): _make_envelope(), Any, Happy path for v3applicationcredential auth., __enter__ must create clouds.yaml at <work_dir>/clouds.yaml., The clouds.yaml file must have permission bits exactly 0o600., clouds.yaml must contain clouds.openstack with…, The env dict for v3applicationcredential must expose all expected OS_* keys., When region_name is in the envelope, both yaml and env include it. (+5 more)

### Community 16 - "Packer Validate Command"
Cohesion: 0.13
Nodes (11): Behaviour of ``PackerExecutor.validate``., validate with no variables runs [packer, validate, '.']., dict variable values are JSON-encoded via json.dumps., list variable values are JSON-encoded via json.dumps., primitive variable values are stringified via str()., validate appends '.' as the final command element after vars., validate returns (True, stdout, stderr) on rc=0., validate returns (False, stdout, stderr) when rc != 0. (+3 more)

### Community 17 - "Fernet Encrypt Decrypt"
Cohesion: 0.14
Nodes (12): decrypt(), encrypt(), Verify the encrypt/decrypt pair preserves arbitrary UTF-8 input., encrypt -> decrypt returns the original ASCII string unchanged., encrypt -> decrypt preserves multi-byte UTF-8 characters end-to-end., encrypt -> decrypt of an empty string returns an empty string., encrypt always returns bytes (Fernet token type)., Fernet's IV randomization yields a different ciphertext for repeated calls. (+4 more)

### Community 18 - "Packer Error Extraction"
Cohesion: 0.10
Nodes (11): Behaviour of ``PackerExecutor._extract_error_from_packer``., TRACE/DEBUG and plugingetter lines are skipped entirely., Lines containing '* Get' are kept as errors., Lines containing 'Error' are kept as errors., Lines containing lowercase 'error' are kept as errors., Lines whose stripped form starts with '*' are kept., When several error lines exist, the first 3 are joined with ' | '., When no error pattern matched, falls back to last non-TRACE/DEBUG line. (+3 more)

### Community 19 - "Terraform Executor"
Cohesion: 0.17
Nodes (10): Any, Executor for Terraform operations with detailed logging. When…, Get environment variables including OpenStack credentials and Terraform debug…, Write ``pg_backend_override.tf`` so init configures the pg backend. No-op if no…, Initialize Terraform in the working directory. Returns: tuple: (success,…, Run terraform apply. Args: var_file: Optional ``-var-file`` value. variables:…, Run terraform destroy. ``refresh=False`` adds ``-refresh=false`` so Terraform…, Get terraform outputs as JSON. (+2 more)

### Community 20 - "Logger Output Formatting"
Cohesion: 0.13
Nodes (12): clean_text(), _now_iso(), Send a progress update without buffering a per-step transcript entry. The…, One streaming line of subprocess output. Skips the buffered transcript…, Current UTC time as ISO-8601 with explicit ``Z`` suffix., Strip ANSI escapes and trim whitespace., clean_text removes ANSI color codes., clean_text trims surrounding whitespace. (+4 more)

### Community 21 - "LogEntry Serialization"
Cohesion: 0.15
Nodes (10): Any, LogEntry.to_dict omits optional fields that are None., LogEntry.to_dict rounds duration_ms to 2 decimal places., LogEntry.to_dict includes streaming=True only when set., LogEntry.to_dict includes truncated=True only when set., LogEntry.to_dict merges extra into top-level but never overwrites., LogEntry.__str__ contains the level icon, timestamp and message., LogEntry.__str__ appends duration in ms when duration_ms is set. (+2 more)

### Community 22 - "Logger Export and Inspection"
Cohesion: 0.12
Nodes (9): get_logs_json(pretty=True) returns parsable indented JSON., get_logs_json(pretty=False) still produces valid JSON., get_logs_text returns one line per entry., get_logs_by_category returns only the matching category., get_logs_by_level returns only entries with that level., get_summary returns counts grouped by level/category and timestamp range., get_summary on an empty buffer reports None for the timestamp range., clear() removes buffered entries and pending operations. (+1 more)

### Community 23 - "Redis Build Lock"
Cohesion: 0.17
Nodes (8): PackerBuildLock, Acquire a Redis lock keyed on (project, image_name). The class is a context-…, Verifies the lock key naming convention., Key includes both project id and image name in the documented order., Key falls back to 'unknown' when project_id is None., Key falls back to 'unknown' when project_id is an empty string., Each lock instance gets a unique token., TestKeyNaming

### Community 24 - "Postgres Backend Override"
Cohesion: 0.15
Nodes (11): _pg_backend_override_hcl(), unit, Tests for the Terraform executor service., Verify pg_backend_override.tf is written only when schema_name is configured., No file is written when backend_schema_name is None., Override file is written and contains the rendered HCL for the schema., Verify the HCL renderer for the pg backend override file., schema_name is embedded inside the backend "pg" block. (+3 more)

### Community 25 - "Failure Exception Serialization"
Cohesion: 0.15
Nodes (9): Failure, Exception, Convert exception data to dict for serialization, Custom exception that carries deployment details for Celery. The full failure…, Reconstruct a Failure from its serialised JSON payload. Used by ``__reduce__``…, Failure exception serialises everything into args[0] for celery., to_dict round-trips through the JSON in ``args[0]``., ``__reduce__`` allows pickling without re-encoding the payload. (+1 more)

### Community 26 - "Crypto and Auth Modules"
Cohesion: 0.18
Nodes (11): Per-task OpenStack credential materialization. The worker receives an encrypted…, _build_cipher(), Symmetric encryption mirror for the worker. Identical surface to…, base64, binascii, contextlib, cryptography_fernet, Fernet (+3 more)

### Community 27 - "Lock Acquire Polling"
Cohesion: 0.14
Nodes (8): acquire_or_wait sleeps for poll_interval_s when waiting on another holder., acquire_or_wait raises TimeoutError when SET fails AND the deadline is past., When pttl returns -2 (expired) or -1 (no TTL), the log helper resolves to None., Verifies the acquire_or_wait polling behavior., acquire_or_wait returns True when SET NX PX succeeds and marks the lock held., Acquiring the lock spawns a daemon heartbeat thread with the documented name…, acquire_or_wait returns False and sleeps when SET fails but deadline isn't…, TestAcquireOrWait

### Community 28 - "OpenStack CLI Runner"
Cohesion: 0.14
Nodes (8): _run returns a helpful message when the openstack binary is missing., _run wraps any other exception into a (-1, '', message) tuple., Tests for the internal _run helper., _run returns (returncode, stdout, stderr) verbatim when subprocess succeeds., _run merges self.env_vars on top of os.environ in the env kwarg., _run coerces None stdout/stderr (e.g. when text=False) to empty strings., _run returns (-1, '', timeout message) when subprocess.TimeoutExpired is raised., TestRun

### Community 29 - "Image Existence Check"
Cohesion: 0.14
Nodes (8): Tests for the image-existence check., Returns (False, None) without invoking the CLI when OS_AUTH_URL is absent., Returns (False, None) without invoking the CLI when env_vars is None., Returns (False, None) when the CLI exits non-zero., Returns (False, None) when CLI returns an empty JSON list., Returns (True, image_id) when exactly one image matches., Returns the first image's ID when multiple matches are returned., TestCheckImageExists

### Community 30 - "Structured Logger Level Methods"
Cohesion: 0.15
Nodes (7): info() appends one entry with level INFO., debug() appends an entry with level DEBUG and category debug., success() appends an entry with level SUCCESS., warning() appends an entry with level WARNING., error() appends an entry with level ERROR., Messages have ANSI sequences removed before being recorded., TestStructuredLoggerLevels

### Community 31 - "Logger Progress Events"
Cohesion: 0.15
Nodes (7): progress() must not append anything to the buffered transcript., progress() emits a PROGRESS event with progress_pct clamped to 0..100., progress() clamps progress_pct to 100 when idx > total., progress() handles total=0 without raising., progress() forwards phase_names as a list., progress() is a noop when no event_emitter is configured., TestStructuredLoggerProgress

### Community 32 - "Logger Operation Timing"
Cohesion: 0.15
Nodes (7): operation_start records an entry and pushes onto the timing stack., operation_end completes a matched op and records a duration_ms., operation_end(success=False) prefixes the message with 'Failed:'., operation_end with no matching start records an entry but no duration., operation_end whose name does not match the stack top leaves the stack alone., track_timing=False means operation_end records no duration., TestStructuredLoggerOperations

### Community 33 - "Git Service"
Cohesion: 0.20
Nodes (7): GitService, Delete the cloned repository., Service for Git operations with HTTPS token authentication., Initialize Git service with settings., Parse Git URL and return components., Convert Git URL to HTTPS format with token authentication., Clone a specific release/tag using shallow clone with HTTPS token…

### Community 34 - "Credential Envelope Error"
Cohesion: 0.18
Nodes (7): CredentialEnvelopeError, Any, Exception, Mirror selected creds into OS_* env vars for tools that ignore clouds.yaml., Raised when the dispatched envelope is missing fields or fails to decrypt., stat, Tests for the PerTaskCloudsConfig context manager.

### Community 35 - "Packer Vars Encoding"
Cohesion: 0.20
Nodes (9): _build_one_packer_image(), encode_packer_vars(), Encode variables for ``packer -var key=value`` CLI args. For HCL…, Build (or reuse) the Packer image for a single template. Skips the build when…, Packer encodes typed lists as JSON arrays (not comma joins)., A list value emits an HCL-compatible JSON array literal., A dict value emits a JSON object literal., Bool values match HCL's lowercase literals. (+1 more)

### Community 36 - "Terraform Compute Instance Extraction"
Cohesion: 0.23
Nodes (7): _extract_compute_instance_ids(), Return server IDs from a terraform pg-backend state dump. Terraform's…, unit, Walk realistic terraform state shapes and surface the server IDs., A best-effort parse — never raises, just yields no IDs., The helper accepts both serialised JSON and pre-parsed dicts.…, TestComputeInstanceExtractor

### Community 37 - "Test Config and Fixtures"
Cohesion: 0.20
Nodes (10): pathlib, tempfile, mock_git_url(), mock_tag(), fixture, Pytest configuration and fixtures., Create a temporary directory for tests., Mock Git URL for testing. (+2 more)

### Community 38 - "Cipher Key Validation"
Cohesion: 0.17
Nodes (7): Verify _build_cipher rejects missing or malformed keys with RuntimeError., _build_cipher raises RuntimeError with a helpful message when the key is empty., _build_cipher treats a None key as missing and raises RuntimeError., _build_cipher wraps Fernet ValueError/TypeError as RuntimeError('malformed')., _build_cipher encodes a str key to bytes before passing to Fernet., _build_cipher passes a bytes key through to Fernet without encoding., TestBuildCipherErrors

### Community 39 - "Clouds YAML Shredding"
Cohesion: 0.17
Nodes (7): __exit__ shreds plaintext and the file., After exiting the context, clouds.yaml must be gone from disk., If the file disappears before __exit__, __exit__ must not raise., Calling _cloud_block after __exit__ must raise 'already shredded'., The private _creds attribute must be set to None after __exit__., The .path attribute must remain readable after shred (so callers can log it)., TestExitAndShredding

### Community 40 - "Terraform Vars Encoding"
Cohesion: 0.17
Nodes (7): Verify the encoding helper produces CLI-safe strings., A dict value is serialised to a JSON object literal, not str()., A list value comes out as a JSON array literal., HCL accepts only lowercase booleans, never Python's ``True``., A top-level ``None`` value is omitted entirely (no -var emitted)., Nested ``None`` keys/items are stripped recursively before encoding., TestEncodeTerraformVars

### Community 41 - "Terraform Env Merging"
Cohesion: 0.17
Nodes (7): Verify _get_env merging rules for env_vars, extra_env, TF_LOG, PG_CONN_STR., env_vars merge on top of os.environ; os.environ keys still present., extra_env passed to _get_env overrides instance env_vars., TF_LOG is propagated from WORKER_TF_LOG when set., TF_LOG is stripped from env if WORKER_TF_LOG is unset, even when inherited., PG_CONN_STR is injected only when backend_conn_str is configured., TestGetEnv

### Community 42 - "Packer Executor Methods"
Cohesion: 0.20
Nodes (6): Any, Build a Packer image. Streams output line-by-line., Extract a meaningful error message from Packer stderr. Packer stderr contains a…, Get environment variables including OpenStack credentials and Packer debug…, Initialize Packer (install required plugins). Streams output., Validate a Packer template (short, buffered).

### Community 43 - "Terraform Output Collection"
Cohesion: 0.18
Nodes (11): collect_terraform_outputs_helper(), collect_terraform_state_helper(), collect_terraform_outputs(), collect_terraform_state(), collect_terraform_state(), Build a TerraformExecutor bound to the deployment's pg-backend schema., Snapshot the terraform state for the task row (best-effort). With the pg…, Collect terraform outputs even on partial success. ``None`` on failure. (+3 more)

### Community 44 - "Text Truncation"
Cohesion: 0.24
Nodes (7): Truncate text intelligently for the buffered transcript. For tool output the…, truncate_text(), truncate_text returns text untouched when under both limits., truncate_text keeps head and tail when line count exceeds max_lines., truncate_text keeps the tail when text exceeds max_chars., truncate_text applies both line and char limits when both exceeded., TestTruncateText

### Community 45 - "OpenStack Server Show"
Cohesion: 0.18
Nodes (7): unit, Tests for the server_show wrapper., Returns the parsed JSON dict when the CLI returns rc=0 and valid JSON., Returns None when the CLI exits non-zero (e.g. server not found)., Returns None when rc=0 but stdout is not valid JSON., Calls the CLI with the expected ``server show`` argument list., TestServerShow

### Community 46 - "Settings Configuration Tests"
Cohesion: 0.20
Nodes (7): dict, unit, Test configuration loading., Test that settings can be imported., Test that required settings attributes exist., Test loading settings from environment variables., TestConfiguration

### Community 47 - "Build Lock Release"
Cohesion: 0.20
Nodes (6): Verifies the release semantics., release() does nothing (no eval) when the lock was never acquired., release() runs the Lua release script with the lock key and token., release() catches exceptions from eval and still flips _held to False., release() stops the heartbeat thread and clears the _heartbeat reference., TestRelease

### Community 48 - "Git URL Parsing"
Cohesion: 0.20
Nodes (6): Test URL parsing functionality., Test parsing SSH Git URL., Test parsing HTTPS Git URL., Test parsing URL without .git extension., Test parsing invalid URL returns None., TestGitServiceURLParsing

### Community 49 - "Logger Entry Construction"
Cohesion: 0.20
Nodes (5): Building an entry whose message exceeds max_chars sets truncated=True., A context kwarg named 'message' is renamed to 'context_message' in extra.…, Slot-named context kwargs are stored on the dataclass field, not in extra., Unknown context kwargs are coerced and merged into the top level., TestStructuredLoggerBuild

### Community 50 - "Password Auth Flow"
Cohesion: 0.20
Nodes (6): Happy path for password (non-v3applicationcredential) auth., Password auth must populate OS_USERNAME/OS_PASSWORD/OS_PROJECT_* and…, clouds.yaml for password auth must hold username/password and project info…, If only user_domain_name is supplied, project_domain_name must mirror it., When project_id/name and domains are absent, neither env nor yaml include them., TestPasswordAuthFlow

### Community 51 - "OpenStack Server Stop"
Cohesion: 0.20
Nodes (6): Tests for the server_stop wrapper., Returns (True, None) when stop exits 0., Returns (False, stripped stderr) when stop exits non-zero., Returns a helpful default message when stop fails with empty stderr., Calls the CLI with ``server stop <id>`` and the 120 s timeout., TestServerStop

### Community 52 - "OpenStack Server Start"
Cohesion: 0.20
Nodes (6): Tests for the server_start wrapper., Returns (True, None) when start exits 0., Returns (False, stripped stderr) when start exits non-zero., Returns a helpful default message when start fails with empty stderr., Calls the CLI with ``server start <id>`` and the 120 s timeout., TestServerStart

### Community 53 - "Terraform Plan Command"
Cohesion: 0.20
Nodes (6): Verify terraform plan command shape., plan with no args uses the bare command and a 300s timeout., plan with var_file appends -var-file and one -var pair per variable., plan non-zero returncode surfaces as success=False., An exception in plan returns (False, "", message)., TestPlan

### Community 54 - "Terraform Destroy Command"
Cohesion: 0.20
Nodes (6): Verify terraform destroy command shape., destroy without args uses bare command with 1800s timeout., destroy adds -var-file and -var pairs., destroy non-zero rc surfaces as success=False., An exception in destroy returns (False, "", message)., TestDestroy

### Community 55 - "Terraform Output Parsing"
Cohesion: 0.20
Nodes (6): Verify terraform output JSON parsing and failure handling., output() parses JSON stdout into a dict on success., output() returns None when terraform exits non-zero., output() returns None when stdout is not valid JSON., output() returns None when subprocess.run raises., TestOutput

### Community 56 - "Decrypt B64 Error Paths"
Cohesion: 0.25
Nodes (6): decrypt_b64(), decrypt_b64 of valid base64 that is not a Fernet token raises InvalidToken., InvalidToken is re-exported from app.utils.crypto.__all__., Verify the decrypt failure modes surface the expected exception types., decrypt_b64 of input that is not valid base64 raises a binascii/ValueError., TestDecryptErrorPaths

### Community 57 - "Git Clone Operations"
Cohesion: 0.28
Nodes (6): patch, Test that existing directory is removed before cloning., Test cleanup on clone failure., Test repository cloning functionality., Test successful repository cloning., TestGitServiceCloning

### Community 58 - "Build Lock Context Manager"
Cohesion: 0.22
Nodes (6): unit, Verifies context manager behavior., __enter__ returns the lock instance itself., __exit__ delegates to release()., Using the lock as a context manager releases it cleanly on block exit., TestContextManager

### Community 59 - "Console Log Sink"
Cohesion: 0.22
Nodes (5): When console=True, entries are forwarded to the stdlib logger., Setting WORKER_LOG_CONSOLE=0 disables the stdlib forwarder., console=False disables the sink even when the env var allows it., ERROR-level entries are routed to logger.error., TestConsoleSink

### Community 60 - "Clouds YAML File Recovery"
Cohesion: 0.22
Nodes (6): unit, File-system robustness: stale files and partial writes., A pre-existing clouds.yaml from a crashed prior task must be replaced., If yaml.safe_dump raises mid-write, the partial file must not remain on disk., Recovery branch (FileExistsError) must also clean up its own partial write., TestStaleFileAndRecovery

### Community 61 - "Build Lock Test Fixtures"
Cohesion: 0.29
Nodes (7): app_services, fake_redis(), fast_lock(), fixture, Tests for the Redis-backed PackerBuildLock service., Patch app.services.build_lock._redis_client to return a MagicMock fake., Create a PackerBuildLock with a tiny heartbeat interval for tests.

### Community 62 - "Redis Build Lock Service"
Cohesion: 0.29
Nodes (6): Redis-backed distributed lock around the Packer image build. Why this exists:…, _redis_client(), Redis, threading, time, uuid

### Community 63 - "Nested None Scrubbing"
Cohesion: 0.29
Nodes (6): Recursively drop ``None`` entries from nested dicts/lists. A stray ``None``…, _scrub_nested_nones(), Recursive None-scrubber walks dicts and lists., ``False`` must survive scrubbing — it's a valid value, not None., ``None`` entries are filtered out of lists., TestScrubNestedNones

### Community 64 - "Base64 Encrypt Decrypt"
Cohesion: 0.29
Nodes (6): encrypt_b64(), unit, Verify the base64-wrapped variant preserves the plaintext., encrypt_b64 yields an ASCII string and decrypt_b64 restores the plaintext., encrypt_b64 / decrypt_b64 preserve non-ASCII UTF-8 input., TestEncryptDecryptB64RoundTrip

### Community 65 - "Heartbeat Loop Tests"
Cohesion: 0.25
Nodes (5): Verifies the heartbeat loop., Heartbeat loop calls eval(_LUA_RENEW, ...) with key, token, and ttl_ms., Heartbeat warns and continues looping when eval raises., Heartbeat exits and unsets _held when renew returns 0 (lock vanished)., TestHeartbeat

### Community 66 - "Git Auth URL Generation"
Cohesion: 0.25
Nodes (5): Test authentication URL generation., Test converting SSH URL to authenticated HTTPS., Test adding token to HTTPS URL., Test authenticated URL has correct format., TestGitServiceAuthentication

### Community 67 - "Terraform State Pull"
Cohesion: 0.25
Nodes (5): Verify terraform state pull return semantics., state_pull() returns stdout verbatim on rc=0., state_pull() returns None on non-zero return code., state_pull() returns None when subprocess.run raises., TestStatePull

### Community 68 - "Crypto Module Reload"
Cohesion: 0.33
Nodes (5): skip, Verify the import-time _build_cipher() call fails when the env key is bad.…, Reloading the module after clearing the key raises RuntimeError at import time., Reloading the module with a malformed key raises RuntimeError('malformed')., TestModuleReloadWithBadKey

### Community 69 - "Logger Factory Tests"
Cohesion: 0.29
Nodes (4): get_logger returns a StructuredLogger instance with correlation id., Each get_logger call returns a new buffer (not memoised)., get_logger().{info,error,success,exception,operation_start/end,command_output,d…, TestGetLoggerSmoke

### Community 70 - "OpenStack Image List"
Cohesion: 0.29
Nodes (4): _CompletedStub, Minimal stand-in for subprocess.CompletedProcess., Returns (False, None) when CLI stdout is not valid JSON., The CLI is invoked with image list, the name filter, and the merged env.

### Community 71 - "Git Integration Tests"
Cohesion: 0.33
Nodes (5): integration, skip, Integration tests (require actual Git access)., Test cloning a real public repository., TestGitServiceIntegration

### Community 72 - "Git Service Test Fixtures"
Cohesion: 0.33
Nodes (5): git_service(), fixture, Tests for Git service., Create GitService instance with temporary base path., unittest_mock

### Community 73 - "Git Cleanup Tests"
Cohesion: 0.33
Nodes (4): Test repository cleanup functionality., Test cleanup of existing directory., Test cleanup handles nonexistent directory gracefully., TestGitServiceCleanup

### Community 74 - "Logger Emitter Error Handling"
Cohesion: 0.33
Nodes (3): progress() must swallow exceptions raised by the emitter., _record swallows emitter errors and still buffers the entry., bad()

### Community 75 - "Task Import Smoke Tests"
Cohesion: 0.33
Nodes (4): unit, Test that tasks can be imported., Test that celery app can be imported., TestTaskBasics

## Knowledge Gaps
- **1 isolated node(s):** `worker`
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 587 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TerraformExecutor` connect `Terraform Executor` to `Deployment Task Lifecycle`, `Terraform State Pull`, `Packer Template Discovery`, `Celery App Configuration`, `Structured Logger Core`, `Terraform Env Merging`, `Terraform Output Collection`, `Subprocess Streaming`, `Terraform Executor Tests`, `Terraform Plan Command`, `Terraform Destroy Command`, `Terraform Output Parsing`, `Postgres Backend Override`?**
  _High betweenness centrality (0.160) - this node is a cross-community bridge._
- **Why does `PerTaskCloudsConfig` connect `OpenStack Credential Context` to `Credential Envelope Error`, `Deployment Task Lifecycle`, `Packer Template Discovery`, `Celery App Configuration`, `Clouds YAML Shredding`, `Clouds YAML Construction`, `Password Auth Flow`, `Crypto and Auth Modules`, `Clouds YAML File Recovery`?**
  _High betweenness centrality (0.139) - this node is a cross-community bridge._
- **Why does `LogCategory` connect `Structured Logger Core` to `Deployment Task Lifecycle`, `Packer Vars Encoding`, `Packer Template Discovery`, `Celery App Configuration`, `Logger Utility Helpers`, `Structured Logger Utilities`, `Terraform Vars Roster`, `Terraform Output Collection`, `Terraform Executor`, `LogEntry Serialization`, `Logger Export and Inspection`?**
  _High betweenness centrality (0.105) - this node is a cross-community bridge._
- **Are the 9 inferred relationships involving `TerraformExecutor` (e.g. with `LogCategory` and `TestApply`) actually correct?**
  _`TerraformExecutor` has 9 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `PerTaskCloudsConfig` (e.g. with `TestApplicationCredentialFlow` and `TestEnvelopeValidation`) actually correct?**
  _`PerTaskCloudsConfig` has 5 INFERRED edges - model-reasoned connections that need verification._
- **Are the 6 inferred relationships involving `StructuredLogger` (e.g. with `slogger()` and `TestConsoleSink`) actually correct?**
  _`StructuredLogger` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Are the 13 inferred relationships involving `deploy_application()` (e.g. with `PackerTemplateDiscoveryError` and `_emit()`) actually correct?**
  _`deploy_application()` has 13 INFERRED edges - model-reasoned connections that need verification._