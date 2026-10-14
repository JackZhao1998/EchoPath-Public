#!/usr/bin/env node
import { Codex } from "@openai/codex-sdk";

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value) {
  return typeof value === "string" && value.trim().length > 0;
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8").trim();
}

function normalizeInput(request) {
  const input = request.input;
  const instructions = nonEmptyString(request.instructions) ? request.instructions.trim() : "";

  if (Array.isArray(input)) {
    const items = [];
    if (instructions) {
      items.push({ type: "text", text: instructions });
    }
    for (const item of input) {
      if (!isPlainObject(item)) {
        continue;
      }
      if (item.type === "text" && nonEmptyString(item.text)) {
        items.push({ type: "text", text: item.text });
      }
      if (item.type === "local_image" && nonEmptyString(item.path)) {
        items.push({ type: "local_image", path: item.path });
      }
    }
    if (items.length > 0) {
      return items;
    }
  }

  const parts = [];
  if (instructions) {
    parts.push(instructions);
  }
  if (typeof input === "string") {
    parts.push(input);
  } else if (input !== undefined && input !== null) {
    parts.push(JSON.stringify(input, null, 2));
  }
  return parts.join("\n\n").trim();
}

function buildThreadOptions(request) {
  const extraBody = isPlainObject(request.extra_body) ? request.extra_body : {};
  const metadata = isPlainObject(request.metadata) ? request.metadata : {};
  const options = {};
  const model = nonEmptyString(request.model) && request.model !== "default" ? request.model : "";

  if (model) {
    options.model = model;
  }
  options.workingDirectory = String(
    extraBody.working_directory || metadata.working_directory || process.cwd(),
  );
  if (nonEmptyString(extraBody.sandbox_mode)) {
    options.sandboxMode = extraBody.sandbox_mode;
  }
  if (typeof extraBody.skip_git_repo_check === "boolean") {
    options.skipGitRepoCheck = extraBody.skip_git_repo_check;
  }
  if (nonEmptyString(request.reasoning_effort)) {
    options.modelReasoningEffort = request.reasoning_effort;
  }
  if (typeof extraBody.network_access_enabled === "boolean") {
    options.networkAccessEnabled = extraBody.network_access_enabled;
  }
  if (nonEmptyString(extraBody.web_search_mode)) {
    options.webSearchMode = extraBody.web_search_mode;
  }
  if (nonEmptyString(extraBody.approval_policy)) {
    options.approvalPolicy = extraBody.approval_policy;
  }
  if (Array.isArray(extraBody.additional_directories)) {
    options.additionalDirectories = extraBody.additional_directories.filter(nonEmptyString);
  }
  return options;
}

async function main() {
  const rawInput = await readStdin();
  const request = rawInput ? JSON.parse(rawInput) : {};
  const extraBody = isPlainObject(request.extra_body) ? request.extra_body : {};

  const codexOptions = {};
  if (isPlainObject(extraBody.codex_config)) {
    codexOptions.config = extraBody.codex_config;
  }
  if (isPlainObject(extraBody.env)) {
    codexOptions.env = Object.fromEntries(
      Object.entries(extraBody.env).map(([key, value]) => [String(key), String(value)]),
    );
  }

  const codex = new Codex(codexOptions);
  const thread = nonEmptyString(extraBody.thread_id)
    ? codex.resumeThread(extraBody.thread_id)
    : codex.startThread(buildThreadOptions(request));

  const turnOptions = {};
  if (isPlainObject(request.output_schema)) {
    turnOptions.outputSchema = request.output_schema;
  }

  const turn = await thread.run(normalizeInput(request), turnOptions);
  process.stdout.write(
    JSON.stringify({
      output_text: turn.finalResponse ?? "",
      model: request.model || "codex",
      response_id: thread.id,
      request_id: null,
      usage: turn.usage || {},
      raw: { thread_id: thread.id },
    }),
  );
}

main().catch((error) => {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`${message}\n`);
  process.exit(1);
});
