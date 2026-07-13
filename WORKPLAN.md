# Step Functions State Machine — Work Plan

This is an AWS account teardown orchestrator state machine implemented as an AWS Service Catalog product.

---

## Step 1: Validate Target Account (OU Check) ✅

**Approach:** Direct AWS SDK integration from Step Functions (no Lambda needed)

**What we're checking:**
- The target account belongs to a specific OU (e.g., a "decommission" OU)
- Blocklist check is NOT needed here — already handled by `nuke-config-base.yaml` filters

**Implementation:**
1. Task state calling `arn:aws:states:::aws-sdk:organizations:listParents`
   - Input: `{ "ChildId": "<target_account_id>" }`
   - Output: Array of parent OUs
2. Choice state evaluating whether the returned parent OU ID matches the expected decommission OU ID
   - Match → proceed to Step 2
   - No match → fail execution with descriptive error ("Account not in decommission OU")

**IAM permissions required on StepFunctionsExecutionRole:**
- `organizations:ListParents`

**Notes:**
- Organizations API is global (us-east-1 endpoint), but Step Functions SDK integration handles routing — no special config needed
- The expected OU ID can be hardcoded in the Choice state condition, or pulled from an SSM parameter if we want it configurable

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
  "RemainingCount": 0,
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

**Error handling — per-item catch (ItemCatcher):**
- Each Lambda invocation has a Catch block so one region failing doesn't abort the entire Map
- On catch, the iterator returns an error result for that region:
```json
{
  "status": "error",
  "region": "<region>",
  "remaining_count": 0,
  "removed_count": 0,
  "failed_resources": [],
  "error": "Lambda invocation failed: <error message>"
}
```
- This allows Step 4/5 to treat failed invocations the same as `resources_remaining` — they'll be retried

**Lambda invocation task state:**
- Resource: `arn:aws:states:::lambda:invoke`
- Parameters:
  - `FunctionName`: nuke-runner Lambda ARN
  - `Payload.$`: the constructed payload from the Pass state
- `ResultSelector` extracts the Lambda response from the `Payload` wrapper:
```json
{
  "status.$": "$.Payload.status",
  "region.$": "$.Payload.region",
  "remaining_count.$": "$.Payload.remaining_count",
  "removed_count.$": "$.Payload.removed_count",
  "failed_resources.$": "$.Payload.failed_resources",
  "error.$": "$.Payload.error"
}
```

**Map state output (array of results, one per region):**
```json
[
  { "status": "complete", "region": "eu-west-1", "remaining_count": 0, "removed_count": 45, "failed_resources": [], "error": null },
  { "status": "resources_remaining", "region": "us-east-1", "remaining_count": 12, "removed_count": 30, "failed_resources": ["s3-bucket-xyz"], "error": null },
  { "status": "error", "region": "ap-southeast-1", "remaining_count": 0, "removed_count": 0, "failed_resources": [], "error": "Lambda timeout" }
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

**⚠️ Review Notes (to address during implementation):**
- **Lambda timeout produces no structured response:** If the nuke-runner Lambda times out (15 min), the invocation fails without returning a payload. The `ItemCatcher` handles the failure, but the hardcoded `"remaining_count": 0` in the catch result is misleading — a timed-out region likely still has resources. Consider using a sentinel value (e.g., `-1`) so Step 5 can distinguish "zero remaining" from "unknown due to error" and always retry error regions.
- **`ItemCatcher` support:** Inline Map `ItemCatcher` was added in 2023. Confirm the CloudFormation tooling/ASL version you're using supports it. If not, fall back to a `Catch` on the entire Map state (less granular).
- **Execution history event limit:** With ~20 regions × multiple retry loops × multiple states per iteration, monitor the 25,000 event history limit. Unlikely to hit with 5 max retries, but worth tracking.

---

## Step 4: Collect Results, Update DynamoDB State Table ✅

**Approach:** Step Functions SDK integration (`dynamodb:UpdateItem`) directly — no Lambda. Iterate over the Map state output (array of per-region results) using a second Map state (sequential) to update each region's row.

**Input (from Step 3 Map state output):**
```json
{
  "results": [
    { "status": "complete", "region": "eu-west-1", "remaining_count": 0, "removed_count": 45, "failed_resources": [], "error": null },
    { "status": "resources_remaining", "region": "us-east-1", "remaining_count": 12, "removed_count": 30, "failed_resources": ["s3-bucket-xyz"], "error": null },
    { "status": "error", "region": "ap-southeast-1", "remaining_count": 0, "removed_count": 0, "failed_resources": [], "error": "Lambda timeout" }
  ],
  "target_account_id": "123456789012",
  "execution_id": "<step-functions-execution-arn>"
}
```

**Iteration:** Sequential Map state over `$.results` array. Each iteration performs a single `dynamodb:UpdateItem` call.

**DynamoDB UpdateItem per region:**

```json
{
  "TableName": "NukeStateTable",
  "Key": {
    "AccountId": { "S.$": "$.target_account_id" },
    "Region": { "S.$": "$.result.region" }
  },
  "UpdateExpression": "SET #status = :status, PreviousRemainingCount = RemainingCount, RemainingCount = :remaining, RemovedCount = :removed, FailedResources = :failed, RunCount = RunCount + :one, LastUpdated = :ts, #error = :err",
  "ConditionExpression": "ExecutionId = :exec_id",
  "ExpressionAttributeNames": {
    "#status": "Status",
    "#error": "Error"
  },
  "ExpressionAttributeValues": {
    ":status": { "S.$": "$.result.normalized_status" },
    ":remaining": { "N.$": "States.Format('{}', $.result.remaining_count)" },
    ":removed": { "N.$": "States.Format('{}', $.result.removed_count)" },
    ":failed": { "L.$": "$.result.failed_resources" },
    ":one": { "N": "1" },
    ":ts": { "S.$": "$$.State.EnteredTime" },
    ":err": { "S.$": "$.result.error" },
    ":exec_id": { "S.$": "$.execution_id" }
  }
}
```

**Status normalization (before the UpdateItem Map):**

Regions that returned `"status": "error"` are treated as `"resources_remaining"` in DynamoDB so they get retried. A Pass state before the update Map normalizes this:

- `"status": "error"` → `"normalized_status": "resources_remaining"`
- `"status": "complete"` → `"normalized_status": "complete"`
- `"status": "resources_remaining"` → `"normalized_status": "resources_remaining"`

This can be done with a Choice + Pass inside the update Map iterator, or with a pre-processing Map that adds `normalized_status` to each result object.

**Implementation — iterator flow inside the update Map:**

```
For each result in results[]:
  → Choice state: Is result.status == "error"?
      - Yes → Pass state: set normalized_status = "resources_remaining"
      - No  → Pass state: set normalized_status = result.status
  → Task state: dynamodb:UpdateItem with the fields above
```

**Progress detection (PreviousRemainingCount):**

The `UpdateExpression` sets `PreviousRemainingCount = RemainingCount` (the old value) BEFORE overwriting `RemainingCount` with the new value. This is a single atomic operation — DynamoDB evaluates the right-hand side of SET using the current item state.

Step 5 can then compare `PreviousRemainingCount` vs `RemainingCount` per region to detect whether progress was made.

**Condition expression (ExecutionId guard):**

`ConditionExpression: "ExecutionId = :exec_id"` ensures we only update rows belonging to the current execution. If a stale/duplicate execution tries to write, the condition fails and the write is rejected (ConditionalCheckFailedException). The Map iterator should catch this error and treat it as a no-op.

**Fields stored per region after update:**

| Attribute | Value |
|-----------|-------|
| `AccountId` | Target account ID (PK) |
| `Region` | Region name (SK) |
| `ExecutionId` | Current Step Functions execution ARN |
| `RunCount` | Incremented by 1 |
| `Status` | `complete` or `resources_remaining` (normalized) |
| `RemainingCount` | Current remaining resources count |
| `PreviousRemainingCount` | Remaining count from the previous run (for progress detection) |
| `RemovedCount` | Resources removed in this run |
| `FailedResources` | List of resource identifiers that failed deletion |
| `LastUpdated` | ISO timestamp |
| `Error` | Error message (null if no error) |

**IAM permissions (StepFunctionsExecutionRole):**
- `dynamodb:UpdateItem` on `NukeStateTable`

**Error handling:**
- `ConditionalCheckFailedException` → caught and treated as no-op (stale execution)
- Other DynamoDB errors → let them propagate up to fail the execution (unexpected)

**Notes:**
- Sequential Map (not parallel) for the updates — order doesn't matter but sequential avoids DynamoDB throttling and keeps it simple
- The update Map's `MaxConcurrency: 1` ensures writes are sequential
- `RemovedCount` is per-run (not cumulative) — cumulative can be derived by summing across RunCount history if needed
- `FailedResources` stores the full list of resource identifiers for debugging/reporting in SNS notifications
- Step 4 does NOT produce a summary — Step 5 queries DynamoDB directly for aggregated state

**⚠️ Review Notes (to address during implementation):**
- **`States.Format` null safety:** `States.Format('{}', $.result.remaining_count)` will fail if `remaining_count` is `null` (e.g., from an error catch block). Ensure Lambda responses always return a numeric value, or add a fallback/default in the Pass state that normalizes error results.
- **`FailedResources` DynamoDB typing:** The expression `":failed": { "L.$": "$.result.failed_resources" }` assumes the value is already in DynamoDB-typed list format (`[{"S": "..."}]`). If the Lambda returns a plain JSON array (`["s3-bucket-xyz"]`), Step Functions SDK integration may not auto-marshal it. Test this — may need `States.JsonToString` or a different approach.
- **`RemovedCount` is per-run:** Since it's overwritten each cycle (not cumulative), the evaluation Lambda in Step 5 can only report the last run's removals per region, not total. Consider `ADD` expression or a separate cumulative attribute if total reporting is needed.

---

## Step 5: Choice State — Retry / Succeed / Fail ✅

**Approach:** Small evaluation Lambda queries DynamoDB, returns a decision object. Choice state branches on the decision. SNS notification built by Step Functions via Pass state. Single SNS topic with message attribute for routing.

**Duplicate execution rejection:**
- EventBridge (or trigger) uses `target_account_id` as the Step Functions execution name
- Step Functions natively rejects `StartExecution` if an execution with that name is already running (`ExecutionAlreadyExists`)
- No extra step needed in the state machine — deduplication is at the trigger level

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
3. Check progress: for each remaining region, compare `RemainingCount < PreviousRemainingCount`
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
    "us-east-1": { "remaining_count": 12, "failed_resources": ["s3-bucket-xyz"] },
    "ap-southeast-1": { "remaining_count": 3, "failed_resources": [] }
  }
}
```

**IAM permissions (evaluation Lambda execution role):**
- `dynamodb:Query` on `NukeStateTable`

---

### 5b: Choice State

Branches based on evaluation Lambda output:

```
Choice state:
  1. $.all_complete == true
       → Go to: SNS Success Notification

  2. $.progress_detected == false (all incomplete regions stuck)
       → Go to: SNS Failure Notification (stuck resources)

  3. $.max_retries_reached == true (RunCount >= 5)
       → Go to: SNS Failure Notification (max retries)

  4. Default (resources remaining + progress + retries left)
       → Go to: Wait State (30 min) → Loop back to Step 3
```

**Evaluation order matters:** Check `all_complete` first, then `no progress` (stuck), then `max retries`, then default to retry.

---

### 5c: Wait State

- **Duration:** 30 minutes (`"Seconds": 1800`)
- **Placement:** After Choice decides to retry, before looping back to Step 3
- **Purpose:** Allow eventual-consistency resources (CloudFront, S3, RDS) to settle before next deletion cycle

---

### 5d: Loop-Back to Step 3

When retrying, the state machine loops back to Step 3 with a modified input:
- `enabled_regions` is replaced by `regions_remaining` from the evaluation Lambda
- `target_account_id`, `target_role_arn`, `no_dry_run` carry forward unchanged

```json
{
  "target_account_id": "123456789012",
  "target_role_arn": "arn:aws:iam::123456789012:role/NukeExecutionRole",
  "enabled_regions": ["us-east-1", "ap-southeast-1"],
  "no_dry_run": true
}
```

**Note:** Only incomplete regions are retried — complete regions are skipped for efficiency.

---

### 5e: SNS Notifications

**Single SNS topic** with message attribute `"outcome"` for subscriber filtering.

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
- Stuck/incomplete regions with remaining count
- Failed resources list per region (from `summary`)
- RunCount reached
- Step Functions execution console link

**IAM permissions (StepFunctionsExecutionRole):**
- `sns:Publish` on the SNS topic ARN
- `lambda:InvokeFunction` on the evaluation Lambda ARN

---

### Step 5 — State Flow Summary

```
Step 4 output
  → Task: Invoke Evaluation Lambda
  → Choice:
      - all_complete → Pass (format success msg) → Task: SNS Publish (success) → End
      - no progress  → Pass (format failure msg) → Task: SNS Publish (failure) → End
      - max retries  → Pass (format failure msg) → Task: SNS Publish (failure) → End
      - default      → Wait (30 min) → Pass (reshape input with regions_remaining) → Step 3
```

**⚠️ Review Notes (to address during implementation):**
- **Evaluation order edge case:** If `max_retries_reached` is true AND `progress_detected` is true, the current order correctly hard-caps retries regardless of progress. Confirm this is intentional.
- **`total_removed` accuracy:** The evaluation Lambda sums `RemovedCount` across regions, but `RemovedCount` is per-run (overwritten each cycle). Either make it cumulative in DynamoDB (`ADD RemovedCount :removed` instead of `SET`) or track a separate `TotalRemovedCount` attribute.
- **EventBridge rule event pattern:** The infra table lists EventBridge but doesn't specify the event source/pattern (e.g., manual trigger, scheduled, or event-driven from another service). Define this during implementation.
- **CloudWatch alarms:** No alarm defined for consecutive failed executions beyond SNS notifications. Consider adding an alarm on the state machine's `ExecutionsFailed` metric.

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
| 11 | StepFunctionsExecutionRole | `AWS::IAM::Role` | ✅ Done — Organizations, Lambda invoke (all), DynamoDB, SNS |
| 12 | ~~EventBridge IAM role~~ | ~~`AWS::IAM::Role`~~ | Removed — not needed |
| 13 | SSM Parameter (nuke config) | `AWS::SSM::Parameter` | Base `nuke-config.yaml` with `PLACEHOLDER_ACCOUNT` token |
