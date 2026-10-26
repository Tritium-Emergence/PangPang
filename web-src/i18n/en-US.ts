// English (en-US) UI strings. Key set MUST match zh-CN.ts exactly (a node guard
// asserts this). Display labels only — never used for state/permission decisions.
export default {
  attention: {needs_attention: 'Needs attention', blocked: 'Blocked', working: 'In progress', done_unread: 'Done, unread', settled: 'Settled', unknown: 'Pending review'},
  task: {queued: 'Queued', pending: 'Pending', running: 'Running', starting: 'Starting', awaiting_approval: 'Awaiting approval', unknown: 'Unknown', cancel_requested: 'Cancelling', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled', blocked: 'Blocked'},
  exchange: {pending: 'Awaiting reply', responded: 'Replied', failed: 'Failed', cancelled: 'Cancelled'},
  capability: {model: 'Chat model', executor: 'Executor', device: 'Device', web_search: 'Web search', semantic_gate: 'Semantic check'},
  capabilityState: {ready: 'Ready', discoveredNotVerified: 'discovered, not verified', unconfigured: 'Not configured', blocked: 'Blocked', unknown: 'Unknown'},
  capabilityAction: {
    restart_coordinator: 'Managed config changed outside the service; restart the coordinator to load the latest config.',
    configure_model: 'Set the endpoint and model name under Settings → Model (keys never pass through chat or the model).',
    reconfigure_model: 'The model is unusable on the current runtime path (credentials unresolved, or invalid endpoint/generation params). Reconfigure under Settings → Model.',
    pair_device: 'Generate a pairing code under Pair device, then run the connector on the target host.',
    bring_device_online: 'All paired devices are offline; start the connector on a device host.',
    set_coordinator_env: 'Save {{var}} from the status bar. The browser encrypts it into the coordinator credential store; the value never enters chat. It applies to this process immediately and is loaded again on the next start. A value set only in the parent process environment still needs a restart. See RUNBOOK.',
    set_deployment_level: 'Start with --deployment-level L0, or set PANGPANG_DEPLOYMENT_LEVEL=L0 in.env.local (L0 is the personal-use tier — see SECURITY.md).',
    connect_executor: 'Connect or check an executor; review the per-item reasons, then re-probe.',
    pair_remote_executor: 'This coordinator is control-only and runs no local executor; pair a separate device as the executor.',
    enable_executor: 'The executor is disabled; re-enable it in config, then re-probe.',
    adjust_executor_policy: 'This bot is not in the executor’s allowed_bots; adjust the executor policy or reassign.',
    generic: 'Action needed; see the docs.',
  },
  work: {phase: {new: 'Just started', delivered: 'Delivered'}, checkpoint: {phase: 'Current phase', summary: 'Progress', completed: 'Completed', next_actions: 'Next', blocked_on: 'Waiting / Blocked'}},
  format: {sizeUnknown: 'Size unknown', empty: '—', daily: 'Daily at {{time}}', weekly: 'Weekly {{weekdays}} {{time}}', interval: 'Every {{minutes}} min', oneShot: 'Once · {{at}}', noTimezone: 'No timezone'},
  errors: {
    operation_failed: 'Operation failed.',
    file_action_rejected: "This computer's client does not support this file operation. Upgrade the connector and retry.",
    local_file_receive_disabled: 'This computer has file receiving disabled. Configure the connector and restart.',
    upload_too_large: 'Each file is at most 8MB.',
    connector_offline: 'This computer is currently offline.',
    connector_operation_pending: 'The computer timed out. Please retry shortly.',
    runner_rejected_request: 'The computer rejected the operation: the path is outside the authorized directory, is not a directory, or the file already exists.',
    transport_unavailable: 'Cannot reach this computer.',
    workspace_overlaps_control_plane: "The authorized directory overlaps PangPang's control directory and was rejected.",
    host_not_found: 'Computer not found.',
    host_no_workspace: 'This device has no executor working directory configured; cannot browse.',
    invalid_upload: 'Invalid file content.',
    invalid_max_concurrency: 'Concurrency must be 1–16.',
    invalid_timeout: 'Timeout must be 30–86400 seconds.',
    invalid_priority: 'Priority must be 1–999, or left blank.',
    installation_busy: 'The executor is still working. Wait for the current task to finish before changing it.',
    installation_config_changed: 'The configuration was updated by another request. Please retry.',
    cwd_outside_allowed_roots: 'The working directory is outside the authorized range or inaccessible.',
    invalid_timeout_s: 'Timeout must be 30–86400 seconds.',
    invalid_use_when: 'The purpose note cannot exceed 500 characters.',
    installation_not_found: 'This executor was not found.',
    invalid_cwd: 'Invalid working directory. Check the path and authorized range.',
    model_response_truncated: 'The model reply exceeded the output cap (output_tokens) and was truncated; this round stopped. Raise the output cap in "Global chat model settings", then send a message to continue.',
    model_endpoint_and_name_required: 'Enter both the model endpoint and the model name.',
    choose_key_or_no_auth: 'Paste an API key, or check "no auth".',
    endpoint_requires_https_or_loopback: 'The model endpoint must be https or a loopback address (http://127.0.0.1).',
    new_endpoint_requires_credential_choice: 'For a new endpoint, choose explicitly: paste an API key, or confirm no auth.',
    model_endpoint_requires_https_or_loopback: 'The model endpoint must be https or a loopback address (http://127.0.0.1).',
    model_endpoint_must_not_contain_credentials: 'The endpoint must not contain credentials; paste the API key in the field below.',
    model_credential_missing: 'This model needs an API key, but it was not found. Please re-enter it.',
    model_tool_call_unverified: 'The model failed the tool-call probe: it may not support function calling. Try another model or check the protocol settings.',
    model_tool_arguments_unverified: 'The model returned unparseable tool-call arguments; this protocol/model combination is not supported. Check the protocol or try another model.',
    model_vision_unverified: 'The model failed the vision probe: it may not accept image input. Turn off "supports images" and retry.',
    model_request_failed: 'Model request failed. Check the endpoint, model name and network.',
    model_rate_limited: 'Model rate-limited (429/quota). Retry later or check your quota.',
    model_timeout: 'The model timed out. Retry later or check whether the endpoint is reachable.',
    model_auth_failed: 'Authentication failed (401/403): the API key may be wrong or expired; a subscription login credential usually cannot be used directly as an API key; use a key issued by the API service. Verify and retry.',
    coordinator_env_not_allowlisted: 'That name cannot be saved here. Only the coordinator keys listed in the status bar are accepted.',
    sealed_credential_required: 'The key has to be encrypted in the browser before it is saved.',
    sealed_credential_rejected: 'The encrypted key could not be opened. Enter it again.',
    invalid_coordinator_env: 'That key was rejected. Use a single-line value.',

    invalid_output_tokens: 'Invalid output cap. Unknown models need at least 4096; lower than that truncates tool calls.',

    output_exceeds_context_window: 'The output cap does not fit in the context window (the prompt still needs room). On a 32K window, set the cap the model actually accepts, for example 8192.',

    invalid_context_window: 'Invalid context window. Use an integer of at least 5120, and not above the catalog window for this model.',
    executor_cleanup_unsupported: 'This device’s supervision tier does not allow this run (reliable stopping is not confirmed). Fix steps are on the task card below.',

  },

  workpane: {

    prevPage: 'Prev', nextPage: 'Next',

    pagerInfo: 'Page {{page}} / {{pages}} · {{total}} loaded',

    artifactPageInfo: 'Page {{page}}', loading: 'Loading…',

    projectsOverview: 'Projects', projectNamePrompt: 'Project name', createProjectFailed: 'Failed to create project',

    newProject: 'New project', noProjects: 'No projects yet.',

    projectWorkCount: 'Work items loaded: {{n}}', projectAttentionCount: '{{n}} still need attention (loaded)',

    routines: 'Routines', noRoutines: 'No routines yet.', worksInProgress: 'Works in progress',

    viewDetail: 'View details', openBot: 'Open bot', continueWork: 'Continue this work',

    executionTasks: 'Tasks', noUnknownTasks: 'No unknown executions awaiting review.', statusUnknown: 'Unknown status',

    unknownTaskNote: 'Unclear whether it ran; will not auto-retry.',

    workspaceIsolated: 'The working directory is isolated; later tasks in the same directory will queue.',

    claimPending: 'Model resource awaiting review', claimNote: 'Can only be released after confirming the server-side request has ended.',

    submitAndRelease: 'Submit review and release',

    claimEvidencePrompt: 'Describe the review process and result (at least 10 characters). Submit only after confirming server-side inference has ended.',

    claimConfirm: 'Release the resource after confirming that inference has stopped?',

    artifacts: 'Deliverables & files', referenceInChat: 'Reference in chat', copyToHost: 'Copy to another host',

    targetHostPrompt: 'Target host ID', targetPathPrompt: 'Target absolute path (must be a new file)',

    snapshotNote: 'Tasks and works come from a bounded snapshot; artifacts use server-side keyset pagination so you can browse history beyond the snapshot window.',

    workDetail: 'Work details', fullRequirement: 'Full requirement', none: '(none)',

    botMessages: 'Bot messages (read-only)', exchangeDetails: 'Exchange details', close: 'Close',

    viewTaskAndLogs: 'View task and logs',

    workspaceIsolatedFull: 'The working directory is isolated; later tasks in the same directory will queue, and it can only be released after the execution boundary is verified.',

    approveExecution: 'Approve this execution', cancel: 'Cancel', reconcile: 'Reconcile result', retry: 'Retry',

    authorityGranted: 'Write access: pre-authorized (executor write tasks are queued automatically)',

    authorityNotGranted: 'Write access: approval required for each executor write task',

    pendingApproval: 'Awaiting approval', pendingTrigger: 'Awaiting trigger', triggered: 'Triggered', paused: 'Paused',

    nextRun: 'Next: ', lastRun: 'Last: ', derivedWork: 'Derived work: {{n}} loaded ',

    requiredHost: 'Required host: ', requiredInstallation: 'Required executor: ',

    pendingApprovalNote: 'Awaiting approval: enable after confirming the instruction, schedule, and authority. Write access is off by default: Executor write tasks will wait for per-task approval.',

    botRole: 'Bot role: ', botInstructions: 'Bot standing instructions: ',

    grantWriteLabel: 'Pre-authorize writes for this routine (skip per-task approval)',

    approveAndEnable: 'Approve and enable', runNow: 'Run now', pause: 'Pause', resume: 'Resume', actionFailed: 'Operation failed',

    confirmNeverStarted: 'Confirm never started', confirmStopped: 'Confirm stopped',

    operatorResolveNoteLabel: 'Audit record: an operator-written human statement. The system did not verify this text.',

    recoveryEvidencePrompt: '{{label}} At least 10 characters.',

    recoveryConfirmNeverStarted: 'Release the working directory after confirming that the task never started?',

    recoveryConfirmStopped: 'Release the working directory after confirming that the executor process has stopped?',

    activityQueued: 'queued at {{time}}',

    activityStarting: 'starting at {{time}}',

    activityRunning: 'started at {{time}}',

    activityLastLog: 'last log {{time}}',

    activityRecent: 'latest step {{step}}',

    activityPresent: 'present {{files}}',

    activityUndeclared: 'no required files declared',

    activityCapped: 'log may have reached its cap',

    activityNone: 'no observable activity yet',
    supervisionWhy: 'This run was refused: the device’s supervision tier does not allow it, and child processes could not be reliably stopped.',
    supervisionLevelHint: 'This device’s execution level is {{level}}, which only allows strong supervision (Windows Job Object).',
    supervisionEnableL0: 'Set this device to L0 (affects this device only)',
    supervisionConsentHint: 'If the level is already L0 and it is still refused, the device has not recorded process-group consent (macOS / cgroup-less Linux). On that device: stop the connector, run the command below interactively and confirm the current risk version (the cloud cannot record it for you), then restart the connector:',
    supervisionThenRetry: 'When done, press "Retry".',
    copy: 'Copy', copied: 'Copied',

  },

  setup: {

    modelDialogTitle: "PangPang's global chat model",

    modelDialogDesc: 'The supervisor and all sub-bots share this endpoint. Tool-calling is verified before saving.',

    modelCatalogLoadFailed: 'Failed to load the model catalog. Use a custom endpoint or reopen settings.',

    validatingToolCalls: 'Verifying tool calls and result continuation…', saveFailed: 'Save failed',

    provider: 'Model provider', providerCustom: 'Custom / local / third-party compatible service', modelName: 'Model name',

    apiProtocol: 'API protocol', protocolOpenaiCompletions: 'OpenAI Chat Completions (for most compatible services)',

    apiBaseUrl: 'API base URL', outputCap: 'Output cap (tokens)',

    contextWindow: 'Context window (tokens)',

    outputCapHint: 'Max tokens generated per reply, minimum {{min}}. Too low truncates tool calls. A 32K window cannot use 32768; set the cap the model accepts and leave room for the prompt.',

    concurrency: 'Simultaneous model conversations',

    concurrencyRestart: 'Concurrency is saved. Restart the coordinator before it runs at the new limit.',

    statusBar: 'Status',

    optionalGate: 'Optional; chat does not depend on it',

    reasoningLevel: 'Reasoning effort', reasoningDefault: 'Server default (no forced toggle)', reasoningOff: 'Off / lowest level the model allows',

    advancedSettings: 'Advanced settings for compatible services',

    advancedHint: "Follow your provider's documentation. A model named Qwen may still use different API parameters; Default sends no thinking parameter.",

    thinkingFormat: 'Thinking param format', thinkingFormatAuto: 'Pi default adapter', budgetField: 'Thinking budget field',

    budgetFieldNone: "Don't override", thinkingBudget: 'Thinking token budget', tokenField: 'Output cap field',

    keepLegacy: 'Keep legacy generation params (only when model and endpoint are unchanged and no reasoning effort is set)',

    endpointHint: 'A custom URL must match the protocol: Anthropic usually takes the root URL, OpenAI usually includes /v1, Gemini usually includes /v1beta.',

    apiKey: 'API key', noAuth: 'This endpoint needs no authentication', vision: 'Model can see images (an image is used to verify on save)',

    keyStorageHint: 'The key is stored in the cloud encrypted credential store and never enters chat.',

    revokeConfirm: 'Delete the saved model API key? Later requests will block until it is set again.',

    keyRevoked: 'Key deleted. Later requests will block until it is set again; requests already sent may still be in progress.',
    visionDisabled: 'Image test failed: the model was saved, but vision is disabled. Re-enable it once the endpoint accepts images.',

    revokeFailed: 'Delete failed', revokeKey: 'Delete saved key', cancel: 'Cancel', validateAndSave: 'Validate and save',

    connectModelTitle: 'First connect a chat model for PangPang', connectModelDesc: 'Set it up once; shared by the supervisor and sub-bots.',

    setupModel: 'Set up model', needsSecretTitle: 'Secure key entry needed', configuringTitle: 'Device is applying setup',

    confirmSetupTitle: 'Confirm device setup', requestActionLine: 'Action: {{action}} · Agent: {{agent}}',

    actionConnectCli: 'Connect CLI', actionRemoveKey: 'Remove managed key', actionExecutorConfig: 'Change executor settings', cwdLine: 'Working directory: {{cwd}}',

    cwdDefault: "The device's authorized default directory", authLine: 'Auth: {{auth}}', authApiKey: 'Dedicated API-key window',

    authExisting: 'Existing CLI login', authUnregistered: 'Not registered', sharedQwen: "Shared Qwen: uses the device's registered resource policy",

    securityBoundary: "Only you can approve the API URL. Bots and file contents cannot choose where your key is sent.",

    apiEndpointOptional: 'API URL (leave blank to use the CLI default endpoint)', operationFailed: 'Operation failed',

    applySettings: 'Apply these settings', denySettings: 'Deny', draftLine: 'Change awaiting confirmation: {{draft}}', cannotOpen: 'Cannot open', openSecretWindow: 'Open key window',
    declareAdapterLine: 'Declare custom adapter: {{adapter}}',
    declareAdapterBoundary: 'Approving stores this exact row. The command is stored as shown.',
    commandLine: 'Command: {{command}}',

    secretDialogTitle: 'Secure key entry', secretSaveHeading: 'Securely save the API key', cliDefaultEndpoint: 'CLI default endpoint',

    secret: 'Key', secretStorageHint: 'The browser encrypts it, then the target device decrypts and stores it. The key never enters chat or the model.',

    encryptAndSave: 'Encrypt and save',

    coordinatorEnvTitle: 'Coordinator keys',
    coordinatorEnvHint: 'The browser encrypts the value for this coordinator. It is stored in the credential vault and applied to this process. It never enters chat. A saved value is loaded again on the next start and replaces an inherited value for that name. A value set only in the parent environment still needs a restart.',
    coordinatorEnvSet: 'Saved',
    coordinatorEnvProcess: 'In this process',
    coordinatorEnvMissing: 'Not set',
    saveCoordinatorEnv: 'Save key',
    updateCoordinatorEnv: 'Update key',
    clearCoordinatorEnv: 'Remove saved key',
    clearCoordinatorEnvConfirm: 'Remove the saved {{var}} from the credential store? This process drops it when it matches the saved value. A parent-environment value returns on the next start.',
    coordinatorEnvDialogTitle: 'Save coordinator key',
    coordinatorEnvPurpose: 'Save {{var}} for this coordinator. The value is encrypted in the browser and does not enter chat.',
    coordinatorEnvStorageHint: 'Stored encrypted in this coordinator’s credential vault and applied to this process. Loaded again on the next start.',

  },

  hosts: {

    connectWorkspace: 'Connect your workspace',

    connectWorkspaceDesc: 'After the connector is paired, the computer appears automatically. Detected executors can be registered from here; each registration completes once the operator approves it.',

    noModelSet: 'No model set', endpointConfigured: 'Endpoint configured', endpointUnconfigured: 'Endpoint not configured',

    setGlobalModel: 'Set global chat model', connectNewComputer: 'Connect new computer', myComputers: 'My computers',

    deviceRevoked: 'Device revoked', online: '● Online', offline: '○ Offline / awaiting connection', localDevice: '● Local',

    osUnregistered: 'OS not registered', asOf: 'as of {{time}}',

    revokeConfirm: "Revoke this device's cloud access? Running tasks will not be force-stopped.", revokeFailed: 'Revoke failed',

    revokeDevice: 'Revoke device access', probed: 'Detected · {{version}}', versionUnregistered: 'Version not registered',
    discoveredNotVerified: 'discovered, not verified · {{version}}', readyVerified: 'Ready · {{version}}',

    unavailable: 'Unavailable · {{reason}}', notProbed: 'Not checked', probeInstall: 'Check installation',

    noExecutors: 'No executors registered yet', pairDialogTitle: 'Connect a computer',
    unregisteredExecutors: 'Detected, not registered', registerExecutor: 'Register executor', registerPending: 'Registration pending approval',

    pairDialogDesc: 'Start the connector on that computer and fill in the cloud URL, allowed working directories, and the pairing code below.',

    pairCodeHint: 'The pairing code is valid for ten minutes and for one device only.', close: 'Close', unsavedChanges: 'Unsaved changes',

    invalidConcurrency: 'Concurrency must be 1–16.', invalidTimeout: 'Timeout must be 30–86400 seconds.',

    invalidPriority: 'Priority must be 1–999, or left blank.', invalidUseWhen: 'The purpose note cannot exceed 500 characters.',

    noChanges: 'No changes to the configuration; nothing to save.', saving: 'Saving…', configSaved: 'Configuration saved',

    saveFailedPrefix: 'Save failed: ', configureExecutor: 'Configure executor', concurrency: 'Concurrency',

    timeoutSeconds: 'Timeout (seconds)', priority: 'Priority', priorityPlaceholder: 'Default', enabled: 'Enabled',

    workingDir: 'Working directory', useWhen: 'When to use', saveConfig: 'Save config',
    executionLevel: 'Execution level',
    executionLevelHint: 'Affects this device only. L0 (personal use): allows the device-proven cooperative supervision tiers (on macOS or cgroup-less Linux the device must also record process-group consent). L1/L2: strong supervision (Windows Job Object) only.',

  },

  chat: {

    botFallback: 'PangPang', executorFallback: 'executor', emptyTitle: 'Tell me what you want to get done.',

    emptyHint: 'Just say what you need and {{name}} will link the relevant work.', thinking: 'Thinking…',

    workingComplex: "This one's a bit involved — I'm on it, results shortly…",

    delegatedTo: 'Handed off to {{name}}: {{text}} — in progress…', dispatchedTo: 'Dispatched to {{executor}}: {{prompt}}, in progress…',

    awaitingResult: 'Waiting for the result…', awaitingApprovalLabel: 'Awaiting your approval', needsConfirm: '⏸ Needs your confirmation before running',

    executorPrefix: 'Executor {{id}}', approve: 'Approve', approveOnce: 'This time only', approveAlways: 'Always', cancel: 'Cancel', needsAttention: 'Needs attention',
    runDevice: 'Device {{name}}', runCommand: 'Command {{command}}', runCwd: 'Working directory {{cwd}}',
    runRoots: 'Allowed directories {{roots}}', runTruncated: 'Command or directories were truncated',
    runSupervision: 'Supervision {{supervision}} · process-group consent {{consent}}', runCredentials: 'Credentials {{ids}}',

    unknownTaskNote: 'Task {{id}} status is unknown; the working directory stays isolated. A stop request needs evidence the execution boundary has halted.',

    stopWork: 'Stop this work', operationFailed: 'Operation failed',

  },

  composer: {

    uploadingWait: 'Files are still uploading. Please wait before sending.', routeFallback: 'Routing is temporarily degraded; continuing on the existing Work.',

    workSwitched: 'Switched to a new Work context.', sendFailed: 'Failed to send', noServer: 'Not connected to the main server yet',

    fileTooLarge: '{{name}}: each file is at most 8MB', pastedImage: 'Pasted image',

    hint: 'Just say what you need; PangPang will link existing work and projects.', uploadFile: 'Upload file', referenceDeviceFile: 'Reference device file',

    uploadingSuffix: ' (uploading…)', removeFile: 'Remove {{name}}', continueTarget: 'Your next message will continue this Work: {{goal}}',

    movePrevious: 'Also move the previous message into this Work',

    placeholder: 'Tell PangPang what you need—a new request, an update, or a change of direction… (Enter to send; Shift+Enter for a new line)', send: 'Send',

  },

  msg: {

    you: 'You', sentTo: 'Sent to {{name}}', from: 'Message from {{name}}', exchange: 'Exchange',

    handoffDetails: 'Handoff details', resultEvidence: 'Result & evidence', botReportNotVerified: "Bot's own report, not independently verified",

    mode: { deliverToUser: 'Deliver to user', returnToSender: 'Return to sender' },

    field: {

      targetBot: 'Handoff to', goal: 'Goal', request: 'Request', acceptance: 'Acceptance criteria', constraints: 'Constraints',

      facts: 'Known facts', completed: 'Completed', openQuestions: 'Open questions', mode: 'Delivery mode', summary: 'Result',

      result: 'Result', executionResult: 'Execution result', artifactRefs: 'Artifacts', outputs: 'Output files',

      evidenceRefs: 'Evidence', error: 'Error', verificationStatus: 'Verification status', sourceWork: 'Source work', other: 'Other',

    },

  },

  inspector: {

    attn: {

      needsAttention: 'Needs my decision', blocked: 'Blocked', working: 'In progress', doneUnread: 'Done, unread',

      settled: 'Settled', unknown: 'Status unconfirmed',

    },

    currentWork: 'Current work', noActiveWork: 'No work in progress yet.', noGoal: '(no goal)', blockedReason: 'Blocked by: ',

    cancelling: 'Cancelling… (waiting for the execution boundary to confirm it stopped)', stopWork: 'Stop this work', needsApproval: '⏸ Needs your approval: {{prompt}}',

    approve: 'Approve', cancel: 'Cancel', unknownStatus: 'Status unknown: {{installation}} · {{host}}. Resolve after verification.',

    reconcile: 'Reconcile result', continueWork: 'Continue this Work', operationFailed: 'Operation failed',

  },

  header: {

    workspaceReady: 'Workspace ready', channelIsolated: 'Execution channel isolated', globalWorking: '{{n}} in progress',

    globalBlocked: '{{n}} blocked', activityTitle: 'Whole-workspace status (not the current bot)', pending: 'Pending {{n}}',

    connecting: 'Connecting…', connectingDot: '● Connecting', connected: '● Connected', reconnecting: '◌ Reconnecting',

    refresh: 'Refresh', menu: 'Menu', defaultBotName: 'PangPang',

  },

  nav: {

    chat: 'Bots & Chat', work: 'Work & Output', hosts: 'My Computers', brand: 'PangPang', partners: 'Long-term partners',

    createBotTitle: 'Create a long-term bot', settingsTitle: 'Edit current bot', settings: 'Settings', busyTitle: 'Server: in progress',

    busy: 'In progress', connected: '● Connected', logout: 'Log out', editRole: 'Edit long-term role', createBot: 'Create long-term bot',

    fieldName: 'Name', fieldRole: 'Role', fieldInstructions: 'Long-term instructions', fieldModel: 'Model', globalModel: 'Global chat model',

    saveFailed: 'Failed to save', archiveConfirm: 'Archive this long-term bot? Existing history will not be deleted.', archiveFailed: 'Failed to archive',

    archiveBot: 'Archive bot', cancel: 'Cancel', save: 'Save',

  },

  file: {

    title: 'Reference a device file', deviceLabel: 'File location', mainServer: 'Main server', onlineDevice: 'Online device',

    workingDir: 'Working directory', up: '↑ Up', truncated: 'Showing first 500 items only.', emptyDir: '(empty folder)',

    loading: 'Loading…', hint: 'Only files inside the executor working directory are browsable; referencing does not modify the original.', selectedPrefix: 'Selected: ',

    pickPrompt: 'Pick a file from the working directory above.', cancel: 'Cancel', reference: 'Reference file',

  },

  attention2: {

    title: 'Pending', needsDecision: 'Needs my decision', blocked: 'Blocked', doneUnread: 'Done, unread',

    noGoal: '(no goal)', routinePending: 'Routine awaiting approval',

    more: '{{n}} more — see Work & Output', empty: 'Nothing needs your attention.',

  },

  media: {

    zoom: 'Zoom image {{alt}}', preview: 'Image preview {{alt}}', closePreview: 'Close image preview', download: 'Download {{name}}',

    tooBig: 'File is large; download to view.', loadFailed: 'Preview failed to load; download to view.',

  },

  login: {

    failed: 'Login failed', headline: 'A place for every piece of work.', subtitle: 'One steward, a team of long-term bots, connected to all your computers.',

    accessKey: 'Access key', enter: 'Enter the studio →',

    keyHint: 'The key is shown once in the coordinator terminal on first start. To see it again, run pangpang cloud --show-access-key on that computer (an interactive terminal is required). The key is never written into chat.',

  },

  facts: { createdAt: 'Created' },

} as const;
