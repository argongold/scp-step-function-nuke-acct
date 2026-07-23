# Step Functions State Machine — Work Plan

This is an AWS account teardown orchestrator state machine implemented as an AWS Service Catalog product.

**State machine input:**
```json
{
  "target_account_id": "123456789012",
  "no_dry_run": true
}
```

The `target_role_arn` is constructed internally using the `TargetRoleName` CloudFormation parameter (default: `NukeExecutionRole`) and the `target_account_id`. A `ConstructTargetRoleArn` Pass state builds the ARN via `States.Format('arn:aws:iam::{}:role/<TargetRoleName>', $.target_account_id)` after the duplicate execution check.

**Dry-run behavior:** When `no_dry_run` is `false`, the state machine completes after a single scan pass (Steps 1–4 + evaluation) without entering the retry loop. All DynamoDB region rows are marked `Status: "dry_run_complete"` before the SNS notification with `outcome=dry_run` is published with the scan summary. This ensures subsequent executions are not blocked by stale rows.

---

## Step 0: Validate Input ✅

**Approach:** Choice state with `IsPresent` and type checks — no Lambda needed.

**What we're checking:**
- `target_account_id` is present and is a string
- `no_dry_run` is present and is a boolean

**Implementation:**
1. Choice state (`ValidateInput`) as the `StartAt` state
   - Condition: `And` rule checking both parameters are present and correctly typed
   - Pass → proceed to Step 1 (ValidateOU)
   - Fail → `FailMissingInput` state with descriptive error

**Fail state:**
- Error: `InputValidationFailed`
- Cause: `Missing or invalid required input. Expected: {"target_account_id": "<string>", "no_dry_run": <boolean>}`

**Notes:**
- This prevents cryptic `States.Runtime` errors from surfacing later in the execution when parameters are missing
- No IAM permissions required — pure Choice/Fail states

---

## Step 1: Validate Target Account (OU Check) ✅

**Approach:** Direct AWS SDK integration from Step Functions (no Lambda needed)

**What we're checking:**
- The target account belongs to a specific OU (e.g., a "quarantine" OU)
- Blocklist check is NOT needed here — already handled by `nuke-config-base.yaml` filters

**Implementation:**
1. Task state (`ValidateOU`) calling `arn:aws:states:::aws-sdk:organizations:listParents`
   - Input: `{ "ChildId": "<target_account_id>" }`
   - Output: Array of parent OUs (stored in `$.ou_result`)
2. Choice state (`CheckOU`) evaluating whether the returned parent OU ID matches the expected quarantine OU ID
   - Match → proceed to Duplicate Execution Check
   - No match → `FailNotInOU` state with descriptive error ("Account is not in the Quarantine OU")

**Fail state:**
- Error: `OUValidationFailed`
- Cause: `Account is not in the Quarantine OU`

**IAM permissions required on StepFunctionsExecutionRole:**
- `organizations:ListParents`

**Notes:**
- Organizations API is global (us-east-1 endpoint), but Step Functions SDK integration handles routing — no special config needed
- The expected OU ID is sourced from the `QuarantineOuId` CloudFormation parameter

---

## Step 1b: Duplicate Execution Check ✅

**Approach:** DynamoDB query to detect if another execution is already processing this account. Prevents concurrent teardowns of the same account.

**What we're checking:**
- No existing DynamoDB rows for this account with `Status` of `pending` or `resources_remaining`
- If active rows exist, another execution is already in progress → fail fast

**Implementation:**
1. Task state (`CheckDuplicateExecution`) calling `arn:aws:states:::aws-sdk:dynamodb:query`
   - Query by `AccountId` (PK) with filter: `Status IN (pending, resources_remaining)`
   - Uses `Select: COUNT` — only needs the count, not full items
   - Result stored in `$.duplicate_check`
2. Choice state (`IsDuplicateExecution`) checking `$.duplicate_check.Count > 0`
   - Count > 0 → `FailDuplicateExecution`
   - Count = 0 → proceed to `ConstructTargetRoleArn`

**DynamoDB query parameters:**
```json
{
  "TableName": "<StateTable>",
  "KeyConditionExpression": "AccountId = :acct",
  "FilterExpression": "#s IN (:pending, :remaining)",
  "ExpressionAttributeNames": { "#s": "Status" },
  "ExpressionAttributeValues": {
    ":acct": { "S.$": "$.target_account_id" },
    ":pending": { "S": "pending" },
    ":remaining": { "S": "resources_remaining" }
  },
  "Select": "COUNT"
}
```

**Fail state:**
- Error: `DuplicateExecutionDetected`
- Cause: `An active execution already exists for this target account`

**IAM permissions required on StepFunctionsExecutionRole:**
- `dynamodb:Query` on the state table

**Notes:**
- This is a DynamoDB-level dedup, complementing any trigger-level dedup (e.g., using `target_account_id` as execution name)
- The `CleanupFailedRows` step (in Step 5) marks regions as `failed` when an execution terminates without completing, ensuring this check doesn't permanently block future retries

---

## Step 2: Discover Enabled Regions in Target Account ✅

**Approach:** Lightweight separate Lambda that assumes the target account role and returns enabled regions

**What it does:**
1. Assumes `NukeExecutionRole` in the target account
2. Calls `account:ListRegions` (filtered for `ENABLED` status)
3. Returns the list of enabled regions

**Lambda input (from Step Functions):**
```json
{
  "target_account_id": "123456789012",
  "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole"
}
```
*(Note: `target_role_arn` is constructed internally by the `ConstructTargetRoleArn` Pass state, not passed as execution input)*

**Lambda output:**
```json
{
  "target_account_id": "123456789012",
  "enabled_regions": ["us-east-1", "eu-west-1", "ap-southeast-1", ...]
}
```

**After Lambda returns, Step Functions writes to DynamoDB state table:**
- Store initial rows for the account — one per region, `Status: "pending"`, `RunCount: 0`
- This seeds the state table before the fan-out begins

**DynamoDB write (per region):**
```json
{
  "AccountId": "123456789012",
  "Region": "<region>",
  "ExecutionId": "<step-functions-execution-arn>",
  "RunCount": 0,
  "Status": "pending",
  "RemainingResCount": 0,
  "LastUpdated": "<iso-timestamp>"
}
```

**IAM permissions:**
- Lambda execution role needs `sts:AssumeRole` on the target role
- Lambda execution role needs `account:ListRegions` (or the target role already has it via AdministratorAccess)
- StepFunctionsExecutionRole needs `lambda:InvokeFunction` on this Lambda
- StepFunctionsExecutionRole needs `dynamodb:PutItem` for the state table writes

**Notes:**
- This Lambda is separate from the nuke-runner Lambda — small, single-purpose (Python or Node, no container needed)
- The DynamoDB writes happen via a Step Functions Map state (iterate over `enabled_regions` array) using direct SDK integration (`dynamodb:putItem`), not inside the Lambda
- This gives us a clean record of which regions were targeted before the fan-out starts

**⚠️ Review Notes (to address during implementation):**
- **DynamoDB seeding adds a sequential Map before the fan-out Map:** Two sequential Map states (seed DynamoDB → fan-out nuke) means more execution history events. With ~20 regions this is fine, but be aware of cumulative event count across retry loops.

---

## Step 3: Map State — Fan-Out Lambda Invocations (One Per Region) ✅

**Approach:** Distributed Map state iterating over `enabled_regions`, invoking the nuke-runner Lambda concurrently for each region. Choice state inside the iterator handles the us-east-1 global resources special case.

**Concurrency:** Unbounded (`MaxConcurrency: 0`). With ~20 enabled regions this is safe. Reserved concurrency on the nuke-runner Lambda should be set to at least 25 to guarantee slots.

**Input to Map state (from Step 2 output):**
```json
{
  "target_account_id": "123456789012",
  "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole",
  "enabled_regions": ["us-east-1", "eu-west-1", "ap-southeast-1", ...],
  "no_dry_run": true
}
```
*(Note: `target_role_arn` is constructed internally by the `ConstructTargetRoleArn` Pass state, not passed as execution input)*

**Map state iterator flow (per item):**

```
For each region in enabled_regions:
  → Choice state: Is region == "us-east-1"?
      - Yes → Pass state: build payload with regions: ["global", "us-east-1"]
      - No  → Pass state: build payload with regions: ["<region>"]
  → Task state: Invoke nuke-runner Lambda with the built payload
```

**Payload construction (Pass states):**

For us-east-1:
```json
{
  "target_account_id.$": "$.target_account_id",
  "target_role_arn.$": "$.target_role_arn",
  "region": "us-east-1",
  "regions": ["global", "us-east-1"],
  "no_dry_run.$": "$.no_dry_run"
}
```

For all other regions:
```json
{
  "target_account_id.$": "$.target_account_id",
  "target_role_arn.$": "$.target_role_arn",
  "region.$": "$.region",
  "regions.$": "States.Array($.region)",
  "no_dry_run.$": "$.no_dry_run"
}
```

**ItemSelector (maps each array element into the iterator's context):**
```json
{
  "region.$": "$$.Map.Item.Value",
  "target_account_id.$": "$.target_account_id",
  "target_role_arn.$": "$.target_role_arn",
  "no_dry_run.$": "$.no_dry_run"
}
```

**Error handling — per-item Catch:**
- Each Lambda invocation task has a `Catch` block (catches `States.ALL`) so one region failing doesn't abort the entire Map
- On catch, a `FormatItemError` Pass state returns a sentinel error result for that region:
```json
{
  "region": "<region>",
  "remaining_res_count": -1,
  "removed_count": 0,
  "resources": []
}
```
- The `-1` sentinel for `remaining_res_count` distinguishes "unknown due to error" from "zero remaining" — Step 4 treats any non-zero value (including -1) as `resources_remaining`, ensuring error regions are always retried

**Lambda invocation task state:**
- Resource: `arn:aws:states:::lambda:invoke`
- Parameters:
  - `FunctionName`: nuke-runner Lambda ARN
  - `Payload.$`: `$` (the full constructed payload from the Pass state)
- `ResultSelector` extracts the Lambda response from the `Payload` wrapper:
```json
{
  "region.$": "$.Payload.region",
  "remaining_res_count.$": "$.Payload.remaining_res_count",
  "removed_count.$": "$.Payload.removed_count",
  "resources.$": "$.Payload.resources"
}
```

**Map state output (array of results, one per region):**
```json
[
  { "region": "eu-west-1", "remaining_res_count": 0, "removed_count": 45, "resources": [] },
  { "region": "us-east-1", "remaining_res_count": 12, "removed_count": 30, "resources": [{"type": "S3Bucket", "id": "s3-bucket-xyz"}] },
  { "region": "ap-southeast-1", "remaining_res_count": -1, "removed_count": 0, "resources": [] }
]
```

**IAM permissions (already covered by StepFunctionsExecutionRole):**
- `lambda:InvokeFunction` on the nuke-runner Lambda ARN

**Notes:**
- The Map state uses Inline mode (not Distributed mode) — we don't need >40 concurrent iterations or child executions for ~20 regions
- `MaxConcurrency: 0` means all regions execute simultaneously
- Lambda timeout (15 min) is acceptable — partial progress is captured in the response, and the retry loop (Step 5) handles continuation
- The `States.Array()` intrinsic function wraps a single value into a one-element array (used for non-us-east-1 regions)
- `ResultPath: null` is NOT used — we need the full results array for Step 4

**⚠️ Review Notes:**
- **Execution history event limit:** With ~20 regions × multiple retry loops × multiple states per iteration, monitor the 25,000 event history limit. Unlikely to hit with 5 max retries, but worth tracking.

---

## Step 4: Collect Results, Update DynamoDB State Table ✅

**Approach:** Step Functions SDK integration (`dynamodb:UpdateItem`) directly — no Lambda. Iterate over the Map state output (array of per-region results) using a second Map state (sequential) to update each region's row.

**Input (from Step 3 Map state output stored in `$.nuke_results`):**
```json
{
  "nuke_results": [
    { "region": "eu-west-1", "remaining_res_count": 0, "removed_count": 45, "resources": [] },
    { "region": "us-east-1", "remaining_res_count": 12, "removed_count": 30, "resources": [{"type": "S3Bucket", "id": "s3-bucket-xyz"}] },
    { "region": "ap-southeast-1", "remaining_res_count": -1, "removed_count": 0, "resources": [] }
  ],
  "target_account_id": "123456789012"
}
```

**Iteration:** Sequential Map state (`UpdateDynamoDB`) over `$.nuke_results` array. `MaxConcurrency: 1`. Each iteration performs status derivation + a single `dynamodb:UpdateItem` call.

**Status derivation (inside the update Map iterator):**

Status is derived from `remaining_res_count` — no `status` field from the Lambda is needed:

- `remaining_res_count == 0` → `derived_status = "complete"`
- `remaining_res_count != 0` (including `-1` error sentinel) → `derived_status = "resources_remaining"`

**Implementation — iterator flow inside the update Map:**

```
For each result in nuke_results[]:
  → Choice state (DeriveStatus): Is result.remaining_res_count == 0?
      - Yes → Pass state (SetStatusComplete): set derived_status = "complete"
      - No  → Pass state (SetStatusRemaining): set derived_status = "resources_remaining"
  → Task state (UpdateRegionItem): dynamodb:UpdateItem
```

**DynamoDB UpdateItem per region:**

```json
{
  "TableName": "<StateTable>",
  "Key": {
    "AccountId": { "S.$": "$.account_id" },
    "Region": { "S.$": "$.result.region" }
  },
  "UpdateExpression": "SET #status = :status, PreviousRemainingResCount = RemainingResCount, RemainingResCount = :remaining, RemovedCount = :removed, Resources = :resources, RunCount = RunCount + :one, LastUpdated = :ts",
  "ConditionExpression": "ExecutionId = :exec_id",
  "ExpressionAttributeNames": {
    "#status": "Status"
  },
  "ExpressionAttributeValues": {
    ":status": { "S.$": "$.derived_status" },
    ":remaining": { "N.$": "States.Format('{}', $.result.remaining_res_count)" },
    ":removed": { "N.$": "States.Format('{}', $.result.removed_count)" },
    ":resources": { "S.$": "States.JsonToString($.result.resources)" },
    ":one": { "N": "1" },
    ":ts": { "S.$": "$$.State.EnteredTime" },
    ":exec_id": { "S.$": "$.execution_id" }
  }
}
```

**Progress detection (PreviousRemainingResCount):**

The `UpdateExpression` sets `PreviousRemainingResCount = RemainingResCount` (the old value) BEFORE overwriting `RemainingResCount` with the new value. This is a single atomic operation — DynamoDB evaluates the right-hand side of SET using the current item state.

Step 5 can then compare `PreviousRemainingResCount` vs `RemainingResCount` per region to detect whether progress was made.

**Condition expression (ExecutionId guard):**

`ConditionExpression: "ExecutionId = :exec_id"` ensures we only update rows belonging to the current execution. If a stale/duplicate execution tries to write, the condition fails and the write is rejected (`ConditionalCheckFailedException`). The iterator catches this error and routes to a `StaleExecutionNoOp` Pass state (no-op).

**Fields stored per region after update:**

| Attribute | Value |
|-----------|-------|
| `AccountId` | Target account ID (PK) |
| `Region` | Region name (SK) |
| `ExecutionId` | Current Step Functions execution ARN |
| `RunCount` | Incremented by 1 |
| `Status` | `complete` or `resources_remaining` (derived from remaining_res_count) |
| `RemainingResCount` | Current remaining resources count (-1 for error regions) |
| `PreviousRemainingResCount` | Remaining count from the previous run (for progress detection) |
| `RemovedCount` | Resources removed in this run |
| `Resources` | JSON string of resource objects (serialized via `States.JsonToString`) |
| `LastUpdated` | ISO timestamp |

**IAM permissions (StepFunctionsExecutionRole):**
- `dynamodb:UpdateItem` on the state table

**Error handling:**
- `DynamoDb.ConditionalCheckFailedException` → caught and routed to `StaleExecutionNoOp` (no-op)
- Other DynamoDB errors → let them propagate up to fail the execution (unexpected)

**Notes:**
- Sequential Map (not parallel) for the updates — order doesn't matter but sequential avoids DynamoDB throttling and keeps it simple
- The update Map's `MaxConcurrency: 1` ensures writes are sequential
- `RemovedCount` is per-run (not cumulative) — cumulative can be derived by summing across RunCount history if needed
- `Resources` is stored as a JSON string (not a DynamoDB List type) using `States.JsonToString` — this avoids DynamoDB type marshalling issues with complex objects
- Step 4 does NOT produce a summary — Step 5 queries DynamoDB directly for aggregated state
- `ResultPath: null` on the Map state — the results are persisted to DynamoDB, not needed in the state payload

**⚠️ Review Notes:**
- **`RemovedCount` is per-run:** Since it's overwritten each cycle (not cumulative), the evaluation Lambda in Step 5 can only report the last run's removals per region, not total. Consider `ADD` expression or a separate cumulative attribute if total reporting is needed.

---

## Step 5: Choice State — Retry / Succeed / Fail ✅

**Approach:** Small evaluation Lambda queries DynamoDB, returns a decision object. Choice state branches on the decision. SNS notification built by Step Functions via Pass state. Single SNS topic with message attribute for routing.

**Duplicate execution rejection:**
- The DynamoDB-based duplicate check in Step 1b queries for active rows (`pending` or `resources_remaining`) before starting
- The `CleanupFailedRows` step (Step 5f) marks incomplete regions as `failed` when an execution terminates, ensuring the dedup check doesn't permanently block future executions
- Additionally, using `target_account_id` as the Step Functions execution name provides native `ExecutionAlreadyExists` rejection at the trigger level

---

### 5a: Evaluation Lambda

**Purpose:** Query DynamoDB, aggregate region statuses, determine retry decision.

**Input (from Step Functions):**
```json
{
  "target_account_id": "123456789012",
  "execution_id": "<step-functions-execution-arn>"
}
```

**Logic:**
1. Query DynamoDB by `AccountId` (PK) — returns all region rows (~20)
2. Categorize regions:
   - `complete` — regions with `Status: "complete"`
   - `remaining` — regions with `Status: "resources_remaining"`
3. Check progress: for each remaining region, compare `RemainingResCount < PreviousRemainingResCount`
4. Determine if at least one remaining region made progress (`progress_detected`)
5. Get current `RunCount` (same across all regions after Step 4)
6. Build filtered `regions_remaining` list (for loop-back to Step 3)

**Output:**
```json
{
  "all_complete": false,
  "progress_detected": true,
  "run_count": 3,
  "max_retries_reached": false,
  "regions_remaining": ["us-east-1", "ap-southeast-1"],
  "regions_complete": ["eu-west-1", "eu-central-1", "..."],
  "total_removed": 245,
  "stuck_regions": [],
  "summary": {
    "us-east-1": { "remaining_res_count": 12, "resources": [{"type": "S3Bucket", "id": "s3-bucket-xyz"}] },
    "ap-southeast-1": { "remaining_res_count": 3, "resources": [] }
  }
}
```

**IAM permissions (evaluation Lambda execution role):**
- `dynamodb:Query` on `NukeStateTable`

---

### 5b: Choice State

Branches based on evaluation Lambda output:

```
Choice state (DecideNextAction):
  0. $.no_dry_run == false (dry run — single pass only)
       → Go to: BuildDryRunRegionsList → CleanupDryRunRows → FormatDryRunMessage → NotifyDryRun (SNS Publish) → End

  1. $.evaluation.all_complete == true
       → Go to: FormatSuccessMessage → NotifySuccess (SNS Publish) → End

  2. $.evaluation.progress_detected == false (all incomplete regions stuck)
       → Go to: CleanupFailedRows → FormatFailureMessage → NotifyFailure (SNS Publish) → End

  3. $.evaluation.max_retries_reached == true (RunCount >= 5)
       → Go to: CleanupFailedRows → FormatFailureMessage → NotifyFailure (SNS Publish) → End

  4. Default (resources remaining + progress + retries left)
       → Go to: WaitBeforeRetry (30 min) → ReshapeForRetry → FanOutNukeRunner (Step 3)
```

**Evaluation order matters:** Check `no_dry_run` first (short-circuit dry runs), then `all_complete`, then `no progress` (stuck), then `max retries`, then default to retry.

---

### 5c: Wait State

- **Duration:** 30 minutes (`"Seconds": 1800`)
- **Placement:** After Choice decides to retry, before looping back to Step 3
- **Purpose:** Allow eventual-consistency resources (CloudFront, S3, RDS) to settle before next deletion cycle

---

### 5d: Loop-Back to Step 3

When retrying, the `ReshapeForRetry` Pass state rebuilds the input and loops back to `FanOutNukeRunner` (Step 3) with a modified payload:
- `region_discovery.enabled_regions` is set to `evaluation.regions_remaining` (only incomplete regions)
- `target_account_id`, `target_role_arn`, `no_dry_run` carry forward unchanged

```json
{
  "target_account_id": "123456789012",
  "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole",
  "no_dry_run": true,
  "region_discovery": {
    "target_account_id": "123456789012",
    "enabled_regions": ["us-east-1", "ap-southeast-1"]
  }
}
```

**Note:** Only incomplete regions are retried — complete regions are skipped for efficiency. The `ReshapeForRetry` state constructs the `region_discovery` object that `FanOutNukeRunner` reads from, bypassing the need to re-run region discovery.

---

### 5e: SNS Notifications

**Single SNS topic** with message attribute `"outcome"` for subscriber filtering.

**Dry run notification:**
```json
{
  "TopicArn": "<sns-topic-arn>",
  "Message": "<formatted message>",
  "Subject": "AWS Nuke Dry Run Complete: Account 123456789012",
  "MessageAttributes": {
    "outcome": { "DataType": "String", "StringValue": "dry_run" }
  }
}
```

**Dry run message content (built by Pass state):**
- Account ID
- Regions scanned (from `regions_complete`)
- Regions with resources found (from `regions_remaining`)
- Scan summary per region
- Step Functions execution console link

**Success notification:**
```json
{
  "TopicArn": "<sns-topic-arn>",
  "Message": "<formatted message>",
  "Subject": "AWS Nuke Complete: Account 123456789012",
  "MessageAttributes": {
    "outcome": { "DataType": "String", "StringValue": "success" }
  }
}
```

**Success message content (built by Pass state):**
- Account ID
- Total regions processed
- Total resources removed (from `total_removed`)
- Execution duration (derived from `$$.Execution.StartTime` vs current time)
- Step Functions execution console link

**Failure notification:**
```json
{
  "TopicArn": "<sns-topic-arn>",
  "Message": "<formatted message>",
  "Subject": "AWS Nuke FAILED: Account 123456789012",
  "MessageAttributes": {
    "outcome": { "DataType": "String", "StringValue": "failure" }
  }
}
```

**Failure message content (built by Pass state):**
- Account ID
- Failure reason: "stuck resources" or "max retries reached (5)"
- Run count reached
- Max retries reached (boolean)
- Progress detected (boolean)
- Stuck regions list
- Regions remaining list
- Summary per region (from `summary`)
- Step Functions execution ID

**IAM permissions (StepFunctionsExecutionRole):**
- `sns:Publish` on the SNS topic ARN
- `lambda:InvokeFunction` on the evaluation Lambda ARN

---

### Step 5 — State Flow Summary

```
Step 4 output
  → Task: Invoke Evaluation Lambda (Evaluate)
  → Choice (DecideNextAction):
      - dry run       → Pass (BuildDryRunRegionsList) → Map (CleanupDryRunRows) → Pass (FormatDryRunMessage) → Task: SNS Publish (NotifyDryRun) → End
      - all_complete  → Pass (FormatSuccessMessage) → Task: SNS Publish (NotifySuccess) → End
      - no progress   → Map (CleanupFailedRows) → Pass (FormatFailureMessage) → Task: SNS Publish (NotifyFailure) → End
      - max retries   → Map (CleanupFailedRows) → Pass (FormatFailureMessage) → Task: SNS Publish (NotifyFailure) → End
      - default       → Wait (WaitBeforeRetry, 30 min) → Pass (ReshapeForRetry) → FanOutNukeRunner (Step 3)
```

---

### 5f: CleanupDryRunRows ✅

**Purpose:** Before sending the dry-run notification, mark all region rows as `Status: "dry_run_complete"` in DynamoDB. This ensures the duplicate execution check (Step 1b) won't block future executions for this account after a dry run.

**Why not reuse `CleanupFailedRows`?**
- `"failed"` is semantically incorrect — the dry run succeeded
- `"complete"` is misleading — resources were not actually deleted (`RemainingResCount > 0`)
- `"dry_run_complete"` clearly indicates a successful scan-only execution

**Implementation:**

1. Pass state (`BuildDryRunRegionsList`) extracts `$.region_discovery.enabled_regions` into `$.all_regions` — this is the full list of all regions (both with and without resources found).

2. Sequential Map state (`CleanupDryRunRows`) over `$.all_regions`, performing a `dynamodb:UpdateItem` per region.

**DynamoDB UpdateItem per region:**
```json
{
  "TableName": "<StateTable>",
  "Key": {
    "AccountId": { "S": "<target_account_id>" },
    "Region": { "S": "<region>" }
  },
  "UpdateExpression": "SET #status = :status, LastUpdated = :ts",
  "ConditionExpression": "ExecutionId = :exec_id",
  "ExpressionAttributeNames": { "#status": "Status" },
  "ExpressionAttributeValues": {
    ":status": { "S": "dry_run_complete" },
    ":ts": { "S": "<timestamp>" },
    ":exec_id": { "S": "<execution_id>" }
  }
}
```

**Notes:**
- Iterates over ALL regions (not just `regions_remaining`) since the dry run should mark everything as terminal
- Uses `ConditionExpression` to only update rows belonging to the current execution
- `ResultPath: null` — cleanup output is not needed downstream
- `"dry_run_complete"` is not matched by Step 1b's filter (`Status IN (pending, resources_remaining)`), so future executions proceed normally

---

### 5g: CleanupFailedRows ✅

**Purpose:** Before sending the failure notification, mark all remaining regions as `Status: "failed"` in DynamoDB. This ensures the duplicate execution check (Step 1b) won't permanently block future executions for this account.

**Implementation:** Sequential Map state over `$.evaluation.regions_remaining`, performing a `dynamodb:UpdateItem` per region.

**DynamoDB UpdateItem per remaining region:**
```json
{
  "TableName": "<StateTable>",
  "Key": {
    "AccountId": { "S.$": "$.account_id" },
    "Region": { "S.$": "$.region" }
  },
  "UpdateExpression": "SET #status = :failed, LastUpdated = :ts",
  "ConditionExpression": "ExecutionId = :exec_id",
  "ExpressionAttributeNames": { "#status": "Status" },
  "ExpressionAttributeValues": {
    ":failed": { "S": "failed" },
    ":ts": { "S.$": "$$.State.EnteredTime" },
    ":exec_id": { "S.$": "$.execution_id" }
  }
}
```

**Notes:**
- Uses `ConditionExpression` to only update rows belonging to the current execution
- `ResultPath: null` — cleanup output is not needed downstream
- This transitions rows from `resources_remaining` → `failed`, which the duplicate check (Step 1b) does not match on

**⚠️ Review Notes:**
- **Evaluation order edge case:** If `max_retries_reached` is true AND `progress_detected` is true, the current order correctly hard-caps retries regardless of progress. Confirm this is intentional.
- **`total_removed` accuracy:** The evaluation Lambda sums `RemovedCount` across regions, but `RemovedCount` is per-run (overwritten each cycle). Either make it cumulative in DynamoDB (`ADD RemovedCount :removed` instead of `SET`) or track a separate `TotalRemovedCount` attribute.

---

## Supporting Infrastructure

### Already Done ✅

| Component | Status | Notes |
|-----------|--------|-------|
| Nuke-runner container image (CodeBuild) | ✅ Done | Container image built via CodeBuild pipeline, pushed to ECR |
| NukeExecutionRole (Target Account) | ✅ Done | Deployed via separate CFN template in target accounts |

---

### To Define in CloudFormation Template (`product.template.yaml`)

All remaining infrastructure lives in eu-west-1 in the service catalog account, defined in a single CloudFormation template:

| # | Component | Type | Notes |
|---|-----------|------|-------|
| 1 | Nuke-runner Lambda | `AWS::Serverless::Function` | ✅ Done — Container image from ECR (ImageUri), 15 min timeout, reserved concurrency 100 |
| 2 | Evaluation Lambda | `AWS::Serverless::Function` | ✅ Done — Python, queries DynamoDB, returns decision object |
| 3 | Region-discovery Lambda | `AWS::Serverless::Function` | ✅ Done — Python, assumes target role, calls `account:ListRegions` |
| 4 | DynamoDB state table | `AWS::DynamoDB::Table` | ✅ Done — PK: `AccountId`, SK: `Region`, on-demand billing |
| 5 | SNS topic | `AWS::SNS::Topic` | ✅ Done — Single topic, message attribute routing for success/failure |
| 6 | Step Functions state machine | `AWS::StepFunctions::StateMachine` | ✅ Done — Steps 1–5 fully implemented |
| 7 | ~~EventBridge rule~~ | ~~`AWS::Events::Rule`~~ | Removed — not needed |
| 8 | NukeLambdaExecutionRole | `AWS::IAM::Role` | ✅ Done — `sts:AssumeRole`, SSM, CloudWatch Logs |
| 9 | EvaluationLambdaExecutionRole | `AWS::IAM::Role` | ✅ Done — `dynamodb:Query`, CloudWatch Logs |
| 10 | RegionDiscoveryLambdaExecutionRole | `AWS::IAM::Role` | ✅ Done — `sts:AssumeRole`, CloudWatch Logs |
| 11 | StepFunctionsExecutionRole | `AWS::IAM::Role` | ✅ Done — Organizations, Lambda invoke (all), DynamoDB (PutItem, UpdateItem, Query), SNS |
| 12 | ~~EventBridge IAM role~~ | ~~`AWS::IAM::Role`~~ | Removed — not needed |
| 13 | SSM Parameter (nuke config) | `AWS::SSM::Parameter` | ✅ Done — Placeholder value, path `/slz-aws-nuke/nuke-config-base` |
