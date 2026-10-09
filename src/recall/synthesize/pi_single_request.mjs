import fs from "node:fs/promises";
import crypto from "node:crypto";
import { pathToFileURL } from "node:url";
import { isDeepStrictEqual } from "node:util";

const controls = {
  mode: "pi-sdk-single-request-v1",
  agent_retries: 0,
  provider_retries: 0,
  automatic_compaction: false,
  cache_warming: "off",
  max_stream_calls: 1,
  tools: false,
  resources: false,
  session_persistence: false,
};
const digest = (data) => crypto.createHash("sha256").update(data).digest("hex");
const emit = (event) => process.stdout.write(JSON.stringify(event) + "\n");
let session;
let unsubscribe;
try {
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  const request = JSON.parse(input);
  if (typeof request.prompt !== "string" || typeof request.system !== "string" ||
      !Number.isSafeInteger(request.max_input_bytes) || request.max_input_bytes < 1) {
    throw new Error("Invalid request");
  }
  const bytes = Buffer.byteLength(request.prompt) + Buffer.byteLength(request.system);
  if (bytes > request.max_input_bytes) throw new Error("Input budget exceeded");
  if (!isDeepStrictEqual(request.policy.controls, controls)) {
    throw new Error("Unsupported policy");
  }
  if (digest(await fs.readFile(request.policy.sdk_module)) !== request.policy.sdk_sha256 ||
      digest(await fs.readFile(new URL(import.meta.url))) !== request.policy.adapter_sha256) {
    throw new Error("Runner identity changed");
  }
  const sdk = await import(pathToFileURL(request.policy.sdk_module).href);
  // Original SDK storage remains authoritative, including normal OAuth refresh.
  const original = sdk.SettingsManager.create(process.cwd(), undefined, {projectTrusted: false});
  const inherited = original.getGlobalSettings();
  const settings = sdk.SettingsManager.inMemory({
    ...inherited,
    retry: {...inherited.retry, enabled: false, maxRetries: 0,
      provider: {...inherited.retry?.provider, maxRetries: 0}},
    compaction: {...inherited.compaction, enabled: false},
    cacheWarming: "off", enableAnalytics: false, enableInstallTelemetry: false,
    defaultTools: [],
  });
  if (settings.getRetrySettings().enabled !== false || settings.getRetrySettings().maxRetries !== 0 ||
      settings.getProviderRetrySettings().maxRetries !== 0 || settings.getCompactionEnabled() !== false ||
      settings.getCacheWarmingMode() !== "off") throw new Error("Policy was not applied");
  const runtime = await sdk.ModelRuntime.create({allowModelNetwork: false});
  const model = runtime.getPhysicalModel(request.provider, request.model_id);
  if (!model) throw new Error("A configured physical chat model is required");
  let calls = 0;
  let violation = false;
  const deny = () => { violation = true; throw new Error("Single-request policy violated"); };
  const stream = runtime.streamSimple.bind(runtime);
  runtime.streamSimple = (selected, context, options) => {
    if (violation || ++calls !== 1 || selected.provider !== request.provider || selected.id !== request.model_id) deny();
    const messages = context.messages.filter((message) => message.role !== "system");
    if (messages.length !== 1 || messages[0].role !== "user" || (context.tools?.length ?? 0) !== 0) deny();
    const content = messages[0].content;
    const text = typeof content === "string" ? content :
      Array.isArray(content) && content.length === 1 && content[0].type === "text" ? content[0].text : undefined;
    if (text !== request.prompt) deny();
    // Do not send SDK-added cwd/date/system context. Supply only approved text.
    return stream(selected, {systemPrompt: request.system,
      messages: [{role: "user", content: request.prompt, timestamp: messages[0].timestamp}], tools: []},
      {...options, maxRetries: 0});
  };
  const resourceLoader = {
    getExtensions: () => ({extensions: [], errors: [], runtime: sdk.createExtensionRuntime()}),
    getSkills: () => ({skills: [], diagnostics: []}),
    getPrompts: () => ({prompts: [], diagnostics: []}),
    getThemes: () => ({themes: [], diagnostics: []}),
    getAgentsFiles: () => ({agentsFiles: []}),
    getSystemPrompt: () => request.system,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
  ({session} = await sdk.createAgentSession({cwd: process.cwd(), modelRuntime: runtime, model,
    thinkingLevel: request.thinking, settingsManager: settings,
    sessionManager: sdk.SessionManager.inMemory(process.cwd()), resourceLoader, tools: [], noTools: "all"}));
  if (session.thinkingLevel !== request.thinking || session.getActiveToolNames().length) deny();
  unsubscribe = session.subscribe((event) => {
    if (/^(auto_retry_|auto_compaction_|cache_warming_|tool_execution_)/.test(event.type)) {
      violation = true;
      void session.abort();
      emit({type: "policy_violation"});
    } else if (event.type === "message_end" && event.message.role === "assistant") {
      emit(event);
    } else if (event.type === "agent_settled") emit({type: "agent_settled"});
  });
  emit({type: "runner_policy", policy: request.policy});
  await session.prompt(request.prompt, {expandPromptTemplates: false});
  if (violation || calls !== 1) throw new Error("Single-request policy violated");
  emit({type: "runner_complete", stream_calls: calls});
} catch {
  // Provider errors can contain credentials or source text; do not echo them.
  process.stderr.write("Pi SDK single-request runner failed.\n");
  process.exitCode = 1;
} finally {
  try {
    unsubscribe?.();
    session?.dispose();
  } catch {
    process.stderr.write("Pi SDK runner cleanup failed.\n");
    process.exitCode = 1;
  }
}
