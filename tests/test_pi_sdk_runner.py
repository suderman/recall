import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_journals import DAY, TZ, workspace
from typer.testing import CliRunner

from recall.cli.main import app
from recall.synthesize import generate

FAKE_SDK = r"""
import fs from 'node:fs';
import path from 'node:path';
const root=process.env.PI_CODING_AGENT_DIR;
const mode=process.env.FAKE_SDK_MODE ?? 'success';
const observed=process.env.FAKE_SDK_OBSERVED;
export const SettingsManager={
 create:()=>({getGlobalSettings:()=>JSON.parse(fs.readFileSync(path.join(root,'settings.json'),'utf8'))}),
 inMemory:settings=>({
  getRetrySettings:()=>({enabled:mode==='ignored-policy'?true:settings.retry.enabled,maxRetries:settings.retry.maxRetries}),
  getProviderRetrySettings:()=>settings.retry.provider,
  getCompactionEnabled:()=>settings.compaction.enabled,
  getCacheWarmingMode:()=>settings.cacheWarming,
 })
};
export const SessionManager={inMemory:()=>({memory:true})};
export function createExtensionRuntime(){return {};}
export const ModelRuntime={create:async options=>{
 if(options.allowModelNetwork!==false)throw Error('catalog networking');
 if('authPath' in options || 'modelsPath' in options)throw Error('do not replace original storage');
 return {
  getPhysicalModel:(provider,id)=>mode==='virtual'?undefined:{provider,id,api:'openai-responses'},
  streamSimple(model,context,options){
   if(options.maxRetries!==0)throw Error('provider retries');
   const trace=fs.existsSync(observed)?JSON.parse(fs.readFileSync(observed,'utf8')):[];
   trace.push({context,authPath:path.join(root,'auth.json'),maxRetries:options.maxRetries});
   fs.writeFileSync(observed,JSON.stringify(trace));
   if(mode==='oauth')fs.writeFileSync(path.join(root,'auth.json'),JSON.stringify({refreshed:'synthetic'}));
   return {role:'assistant',provider:model.provider,model:model.id,
    stopReason:mode==='model-error'?'error':'stop',
    content:[{type:'text',text:'Updated.[fn:evt_mail]'}],usage:{input:4,output:3},timestamp:1};
  }
 };
}};
export async function createAgentSession(options){
 if(options.tools.length || options.noTools!=='all' || !options.sessionManager.memory)
  throw Error('tools or persistence');
 const loader=options.resourceLoader;
 if(loader.getExtensions().extensions.length || loader.getSkills().skills.length ||
    loader.getAgentsFiles().agentsFiles.length)throw Error('resources');
 let callback=()=>{};
 return {session:{
  thinkingLevel:options.thinkingLevel,
  getActiveToolNames:()=>mode==='tools'?['bash']:[],
  subscribe:fn=>{callback=fn;return ()=>{};}, abort:async()=>{}, dispose:()=>{},
  async prompt(prompt){
   const extra=['auto_retry_start','auto_compaction_start',
                'cache_warming_start','tool_execution_start'];
   if(extra.includes(mode)) callback({type:mode});
   const user={role:'user',timestamp:1,
               content:[{type:'text',text:mode==='changed-context'?'UNAPPROVED':prompt}]};
   const context={systemPrompt:'UNAPPROVED SDK DEFAULT',tools:[],
                  messages:[{role:'system',content:'UNAPPROVED'},user]};
   const message=await options.modelRuntime.streamSimple(options.model,context,{});
   if(mode==='second-call'){
    try{await options.modelRuntime.streamSimple(options.model,context,{});}catch{}
   }
   callback({type:'message_end',message});
   if(mode==='multi-message')callback({type:'message_end',message});
   callback({type:'agent_settled'});
  }
 }};
}
"""


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional SDK bridge tests require Node")
    original = tmp_path / "original-agent"
    original.mkdir()
    for name, value in {
        "auth.json": {"synthetic": {"type": "api_key", "key": "not-a-real-credential"}},
        "models.json": {
            "providers": {
                "synthetic": {
                    "api": "openai-responses",
                    "baseUrl": "https://invalid.example/v1",
                    "models": [
                        {
                            "id": "fixture",
                            "name": "Synthetic fixture",
                            "reasoning": False,
                            "input": ["text"],
                            "contextWindow": 200000,
                            "maxTokens": 1000,
                            "cost": {"input": 5, "output": 5, "cacheRead": 1, "cacheWrite": 1},
                        }
                    ],
                }
            }
        },
        "settings.json": {
            "retry": {"enabled": True, "maxRetries": 3, "provider": {"maxRetries": 3}},
            "compaction": {"enabled": True},
            "cacheWarming": "streaming",
        },
    }.items():
        (original / name).write_text(json.dumps(value))
        (original / name).chmod(0o600)
    module = tmp_path / "fake-sdk.mjs"
    module.write_text(FAKE_SDK)
    observed = tmp_path / "observed.json"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(original))
    monkeypatch.setenv("FAKE_SDK_OBSERVED", str(observed))
    return Path(node).resolve(), module, original, observed


def test_sdk_policy_and_exact_provider_context_without_credential_copy(sdk):
    node, module, original, observed = sdk
    before = {p: p.read_bytes() for p in original.iterdir()}
    policy = generate._sdk_policy(node, module)
    body, metadata = generate.run_pi_sdk(
        "Synthetic user prompt", "synthetic/fixture:off", policy, 131072
    )
    assert body == "Updated.[fn:evt_mail]"
    assert metadata["runner"] == "pi-sdk-single-request-v1"
    assert metadata["runner_policy"] == policy
    trace = json.loads(observed.read_bytes())
    assert len(trace) == 1 and trace[0]["authPath"] == str(original / "auth.json")
    assert trace[0]["context"]["systemPrompt"] == generate.SYSTEM_PROMPT
    assert trace[0]["context"]["messages"][0]["content"] == "Synthetic user prompt"
    assert trace[0]["context"]["tools"] == []
    assert metadata["runner_request"]["request_bytes"] == len(
        "Synthetic user prompt".encode()
    ) + len(generate.SYSTEM_PROMPT.encode())
    assert {p: p.read_bytes() for p in original.iterdir()} == before


@pytest.mark.parametrize(
    "mode",
    [
        "ignored-policy",
        "virtual",
        "tools",
        "changed-context",
        "second-call",
        "auto_retry_start",
        "auto_compaction_start",
        "cache_warming_start",
        "tool_execution_start",
        "multi-message",
        "model-error",
    ],
)
def test_sdk_refuses_unsafe_or_failed_session_without_second_stream(sdk, monkeypatch, mode):
    node, module, original, observed = sdk
    before = {p: p.read_bytes() for p in original.iterdir()}
    monkeypatch.setenv("FAKE_SDK_MODE", mode)
    with pytest.raises(ValueError):
        generate.run_pi_sdk(
            "Synthetic", "synthetic/fixture:off", generate._sdk_policy(node, module), 131072
        )
    assert not observed.exists() or len(json.loads(observed.read_bytes())) <= 1
    assert {p: p.read_bytes() for p in original.iterdir()} == before


def test_sdk_auth_refresh_uses_original_store_without_duplicate_files(sdk, monkeypatch):
    node, module, original, observed = sdk
    unchanged = {p: p.read_bytes() for p in (original / "settings.json", original / "models.json")}
    monkeypatch.setenv("FAKE_SDK_MODE", "oauth")
    generate.run_pi_sdk(
        "Synthetic", "synthetic/fixture:off", generate._sdk_policy(node, module), 131072
    )
    assert json.loads((original / "auth.json").read_bytes()) == {"refreshed": "synthetic"}
    assert {p: p.read_bytes() for p in unchanged} == unchanged
    assert len(list(module.parent.rglob("auth.json"))) == 1


def test_sdk_budget_prevents_process_before_model_or_config_read(sdk):
    node, module, _, observed = sdk
    with pytest.raises(ValueError, match="budget"):
        generate.run_pi_sdk(
            "Synthetic", "synthetic/fixture:off", generate._sdk_policy(node, module), 1
        )
    assert not observed.exists()


def test_sdk_invalid_paths_never_fall_back(tmp_path):
    with pytest.raises(ValueError):
        generate._sdk_policy(None, tmp_path / "sdk.mjs")
    with pytest.raises(ValueError):
        generate._sdk_policy(Path("node"), Path("sdk.mjs"))


def test_sdk_cache_policy_and_reviewed_publication(sdk, tmp_path):
    node, module, _, observed = sdk
    paths = workspace(tmp_path / "recall")
    output = tmp_path / "previews"
    kwargs: dict[str, Any] = dict(
        first=DAY,
        last=DAY,
        author="Example",
        timezone_name=TZ,
        output=output,
        model="synthetic/fixture:off",
        node_executable=node,
        pi_sdk=module,
    )
    first = generate.build_journals(paths, **kwargs)[0]
    assert first["status"] == "generated"
    record, _ = generate._read_revision(Path(first["revision"]))
    assert record["generation_options"]["runner_policy"] == generate._sdk_policy(node, module)
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "cached"
    reviewed = generate.publish_journal(
        paths, revision=Path(first["revision"]), output=output, draft="Reviewed.[fn:evt_mail]"
    )
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "cached"
    assert len(json.loads(observed.read_bytes())) == 1
    assert Path(reviewed["revision"]).exists()
    module.write_text(module.read_text() + "\n// A new SDK identity.\n")
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "generated"
    assert len(json.loads(observed.read_bytes())) == 2


def test_sdk_and_legacy_fingerprints_are_separate(sdk, tmp_path, monkeypatch):
    node, module, _, _ = sdk
    paths = workspace(tmp_path / "recall")
    output = tmp_path / "previews"
    kwargs: dict[str, Any] = dict(
        first=DAY,
        last=DAY,
        author="Example",
        timezone_name=TZ,
        output=output,
        model="synthetic/fixture:off",
    )
    generate.build_journals(paths, **kwargs, node_executable=node, pi_sdk=module)
    calls = []
    monkeypatch.setattr(
        generate, "run_pi", lambda *args: (calls.append(args) or "Legacy.[fn:evt_mail]", {})
    )
    assert generate.build_journals(paths, **kwargs)[0]["status"] == "generated" and len(calls) == 1
    assert (
        generate.build_journals(paths, **kwargs, node_executable=node, pi_sdk=module)[0]["status"]
        == "generated"
    )


@pytest.mark.parametrize(
    "damage",
    ["event-type", "boolean-count", "two-settled", "order", "missing-policy", "extra-event"],
)
def test_sdk_protocol_failures_are_safe_values(sdk, monkeypatch, damage):
    node, module, _, _ = sdk

    def response(args, **kwargs):
        request = json.loads(kwargs["input"])
        message = {
            "role": "assistant",
            "provider": "synthetic",
            "model": "fixture",
            "stopReason": "stop",
            "content": [{"type": "text", "text": "Synthetic"}],
        }
        rows = [
            {"type": "runner_policy", "policy": request["policy"]},
            {"type": "message_end", "message": message},
            {"type": "agent_settled"},
            {"type": "runner_complete", "stream_calls": 1},
        ]
        if damage == "event-type":
            rows.insert(1, {"type": None})
        elif damage == "boolean-count":
            rows[-1]["stream_calls"] = True
        elif damage == "two-settled":
            rows.insert(2, {"type": "agent_settled"})
        elif damage == "order":
            rows[-1], rows[-2] = rows[-2], rows[-1]
        elif damage == "missing-policy":
            rows.pop(0)
        else:
            rows.insert(2, {"type": "auto_retry_start"})
        return subprocess.CompletedProcess(args, 0, "\n".join(json.dumps(row) for row in rows), "")

    monkeypatch.setattr(generate.subprocess, "run", response)
    with pytest.raises(ValueError, match="policy"):
        generate.run_pi_sdk(
            "Synthetic", "synthetic/fixture:off", generate._sdk_policy(node, module), 131072
        )


@pytest.mark.parametrize("mode", ["success", "transient-error", "context-error"])
def test_installed_sdk_with_fake_provider_only(sdk, monkeypatch, mode):
    installed = os.environ.get("RECALL_TEST_PI_SDK_MODULE")
    if not installed:
        pytest.skip(
            "Set an explicit installed SDK module to run optional real-SDK/fake-stream tests"
        )
    node, module, original, observed = sdk
    before = {p: p.read_bytes() for p in original.iterdir()}
    source = r"""
import fs from 'node:fs';
import path from 'node:path';
globalThis.fetch=()=>{throw Error('No network in installed-SDK test');};
const actual=await import(__SDK__);
export const SettingsManager=actual.SettingsManager;
export const SessionManager=actual.SessionManager;
export const createExtensionRuntime=actual.createExtensionRuntime;
export const createAgentSession=actual.createAgentSession;
export const ModelRuntime={create:async options=>{
 const runtime=await actual.ModelRuntime.create({...options,refreshOnCreate:false,
  modelsStorePath:path.join(process.env.PI_CODING_AGENT_DIR,'test-catalog.json')});
 runtime.streamSimple=(model,context,options)=>{
  const file=process.env.FAKE_SDK_OBSERVED;
  const rows=fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):[];
  rows.push({context,maxRetries:options.maxRetries});fs.writeFileSync(file,JSON.stringify(rows));
  const mode=process.env.FAKE_SDK_MODE;
  const success=mode==='success';
  const message={role:'assistant',api:model.api,provider:model.provider,model:model.id,
   content:success?[{type:'text',text:'Synthetic completed response.'}]:[],
   usage:{input:250000,output:1,cacheRead:0,cacheWrite:0,totalTokens:250001,
          cost:{input:1,output:0,cacheRead:0,cacheWrite:0,total:1}},timestamp:1,
   stopReason:success?'stop':'error',
   ...(success?{}:{errorMessage:mode==='transient-error'?
     '503 Service Unavailable':'context length exceeded'})};
  return {async *[Symbol.asyncIterator](){yield {type:'start',partial:message};
    yield success?{type:'done',reason:'stop',message}:{type:'error',reason:'error',error:message};},
    result:async()=>message};
 };
 return runtime;
}};
"""
    module.write_text(source.replace("__SDK__", json.dumps(Path(installed).resolve().as_uri())))
    monkeypatch.setenv("FAKE_SDK_MODE", mode)
    policy = generate._sdk_policy(node, module)
    if mode == "success":
        body, _ = generate.run_pi_sdk(
            "Synthetic evidence only.", "synthetic/fixture:off", policy, 131072
        )
        assert body == "Synthetic completed response."
    else:
        with pytest.raises(ValueError):
            generate.run_pi_sdk("Synthetic evidence only.", "synthetic/fixture:off", policy, 131072)
    assert len(json.loads(observed.read_bytes())) == 1
    assert {p: p.read_bytes() for p in before} == before


def test_sdk_cli_selects_explicit_bridge(sdk, tmp_path):
    node, module, _, observed = sdk
    paths = workspace(tmp_path / "recall")
    result = CliRunner().invoke(
        app,
        [
            "journal",
            "build",
            "--root",
            str(paths.root),
            "--from",
            DAY,
            "--to",
            DAY,
            "--author",
            "Example",
            "--timezone",
            TZ,
            "--output",
            str(tmp_path / "previews"),
            "--model",
            "synthetic/fixture:off",
            "--node-executable",
            str(node),
            "--pi-sdk",
            str(module),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(json.loads(observed.read_bytes())) == 1
