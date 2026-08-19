import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { promisify } from "node:util";

import { joinSession } from "@github/copilot-sdk/extension";

const execFileAsync = promisify(execFile);
const settingsPath = join(homedir(), ".copilot", "np-agent-memory", "settings.json");

const DEFAULT_SETTINGS = Object.freeze({
    enabled: true,
    pollIntervalSeconds: 60,
    promptMode: "prompt-on-urgent",
});

const state = {
    active: true,
    idle: false,
    hasObservedWork: false,
    pollInFlight: false,
    retryRequested: false,
    pollStartPromise: null,
    restartRequested: false,
    pollingDisabled: false,
    registrationWarningLogged: false,
    configurationWarningLogged: false,
    registrationEpoch: 0,
    workingDirectoryEpoch: 0,
    hasSessionStart: false,
    seenMessageIds: new Set(),
    pendingPrompt: null,
    timer: null,
    workingDirectory: null,
};

let session;

function setWorkingDirectory(workingDirectory, authoritative = false) {
    if (state.hasSessionStart && !authoritative) {
        return;
    }

    if (authoritative) {
        state.hasSessionStart = true;
    }

    if (state.workingDirectory === workingDirectory) {
        return;
    }

    state.workingDirectory = workingDirectory;
    state.workingDirectoryEpoch += 1;
    state.pollingDisabled = false;
}

function normalizeSettings(value) {
    const configured = value?.inboxNotifier;
    if (!configured || typeof configured !== "object" || Array.isArray(configured)) {
        return DEFAULT_SETTINGS;
    }

    const promptMode = ["notify", "prompt-on-urgent", "prompt-on-any"].includes(
        configured.promptMode,
    )
        ? configured.promptMode
        : DEFAULT_SETTINGS.promptMode;
    const pollIntervalSeconds = Number.isInteger(configured.pollIntervalSeconds)
        ? Math.min(Math.max(configured.pollIntervalSeconds, 10), 3600)
        : DEFAULT_SETTINGS.pollIntervalSeconds;

    return {
        enabled:
            typeof configured.enabled === "boolean"
                ? configured.enabled
                : DEFAULT_SETTINGS.enabled,
        pollIntervalSeconds,
        pluginRoot:
            typeof configured.pluginRoot === "string" &&
            isAbsolute(configured.pluginRoot)
                ? configured.pluginRoot
                : null,
        promptMode,
    };
}

async function loadSettings() {
    try {
        return normalizeSettings(JSON.parse(await readFile(settingsPath, "utf8")));
    } catch (error) {
        if (error?.code === "ENOENT") {
            return DEFAULT_SETTINGS;
        }
        await session.log(
            `np-agent-memory inbox notifier ignored invalid settings at ${settingsPath}: ${error.message}`,
            { level: "warning" },
        );
        return DEFAULT_SETTINGS;
    }
}

async function getInboxSummary(agentCwd, pluginRoot) {
    if (!pluginRoot) {
        const error = new Error(
            "the notifier plugin root is not configured. Run install-inbox-notifier.ps1 from the plugin directory.",
        );
        error.code = "NP_AGENT_MEMORY_PLUGIN_ROOT_NOT_CONFIGURED";
        throw error;
    }

    const { stdout } = await execFileAsync(
        "uvx",
        [
            "--from",
            pluginRoot,
            "np-agent-memory",
            "inbox-summary",
            "--agent-cwd",
            agentCwd,
        ],
        {
            windowsHide: true,
            timeout: 15000,
            maxBuffer: 1024 * 1024,
        },
    );
    return JSON.parse(stdout);
}

function describeInbox(summary) {
    const urgent = summary.urgent_unread_count;
    return `${summary.unread_count} unread inbox message${summary.unread_count === 1 ? "" : "s"}${urgent ? ` (${urgent} urgent)` : ""}`;
}

async function flushPrompt() {
    if (
        !state.pendingPrompt ||
        !state.active ||
        (state.hasObservedWork && !state.idle)
    ) {
        return;
    }

    if (state.timer) {
        return;
    }

    const prompt = state.pendingPrompt;
    state.pendingPrompt = null;
    state.idle = false;
    try {
        await session.send({ prompt });
    } catch (error) {
        await session.log(
            `np-agent-memory inbox prompt could not be sent: ${error.message}`,
            { level: "warning" },
        );
    }
}

async function pollInbox() {
    if (
        !state.active ||
        !state.workingDirectory ||
        state.pollingDisabled ||
        state.pollInFlight
    ) {
        return;
    }

    state.pollInFlight = true;
    const registrationEpoch = state.registrationEpoch;
    const workingDirectoryEpoch = state.workingDirectoryEpoch;
    const workingDirectory = state.workingDirectory;
    try {
        const settings = await loadSettings();
        if (!settings.enabled) {
            return;
        }

        const summary = await getInboxSummary(
            workingDirectory,
            settings.pluginRoot,
        );
        if (workingDirectoryEpoch !== state.workingDirectoryEpoch) {
            state.retryRequested = true;
            return;
        }
        const messageIds = new Set(summary.messages.map((message) => message.id));
        const newMessages = summary.messages.filter(
            (message) => !state.seenMessageIds.has(message.id),
        );
        state.seenMessageIds = messageIds;

        if (newMessages.length === 0) {
            return;
        }

        await session.log(`np-agent-memory: ${describeInbox(summary)}.`);

        const hasUrgentMessage = newMessages.some(
            (message) => message.priority === "urgent",
        );
        const shouldPrompt =
            settings.promptMode === "prompt-on-any" ||
            (settings.promptMode === "prompt-on-urgent" && hasUrgentMessage);
        if (shouldPrompt) {
            state.pendingPrompt =
                `You have ${describeInbox(summary)}. ` +
                "Read it with np-agent-memory-inbox_check before continuing if it affects current work.";
            await flushPrompt();
        }
    } catch (error) {
        if (error?.code === "NP_AGENT_MEMORY_PLUGIN_ROOT_NOT_CONFIGURED") {
            state.pollingDisabled = true;
            if (state.timer) {
                clearInterval(state.timer);
                state.timer = null;
            }
            if (!state.configurationWarningLogged) {
                state.configurationWarningLogged = true;
                await session.log(
                    `np-agent-memory inbox notifier is disabled: ${error.message}`,
                    { level: "warning" },
                );
            }
            return;
        }
        if (/not registered/i.test(error.message)) {
            if (
                registrationEpoch !== state.registrationEpoch ||
                workingDirectoryEpoch !== state.workingDirectoryEpoch
            ) {
                state.retryRequested = true;
                return;
            }
            state.pollingDisabled = true;
            if (state.timer) {
                clearInterval(state.timer);
                state.timer = null;
            }
            if (!state.registrationWarningLogged) {
                state.registrationWarningLogged = true;
                await session.log(
                    "np-agent-memory inbox notifier is waiting for agent registration.",
                    { level: "warning" },
                );
            }
            return;
        }
        await session.log(
            `np-agent-memory inbox notifier check failed: ${error.message}`,
            { level: "warning" },
        );
    } finally {
        state.pollInFlight = false;
        if (state.retryRequested) {
            state.retryRequested = false;
            void pollInbox();
        }
    }
}

async function startPolling() {
    if (
        !state.active ||
        !state.workingDirectory ||
        state.pollingDisabled
    ) {
        return;
    }

    if (state.pollStartPromise) {
        state.restartRequested = true;
        await state.pollStartPromise;
        return;
    }

    state.pollStartPromise = (async () => {
        const settings = await loadSettings();
        if (
            !state.active ||
            !state.workingDirectory ||
            state.timer ||
            !settings.enabled
        ) {
            return;
        }
        state.timer = setInterval(() => {
            void pollInbox();
        }, settings.pollIntervalSeconds * 1000);
        await pollInbox();
    })();

    try {
        await state.pollStartPromise;
    } finally {
        state.pollStartPromise = null;
    }

    if (state.restartRequested) {
        state.restartRequested = false;
        await startPolling();
    }
}

async function initializeSession() {
    try {
        const metadata = await session.rpc.metadata.snapshot();
        setWorkingDirectory(metadata.workingDirectory);
        await startPolling();
    } catch (error) {
        await session.log(
            `np-agent-memory inbox notifier could not determine the session directory: ${error.message}`,
            { level: "warning" },
        );
    }
}

session = await joinSession({
    hooks: {
        onSessionStart: async (input) => {
            setWorkingDirectory(input.workingDirectory, true);
            await startPolling();
        },
        onUserPromptSubmitted: async () => {
            state.hasObservedWork = true;
            state.idle = false;
        },
        onPostToolUse: async (input) => {
            if (input.toolName === "np-agent-memory-agent_register") {
                state.registrationEpoch += 1;
                state.pollingDisabled = false;
                state.registrationWarningLogged = false;
                state.configurationWarningLogged = false;
                void startPolling();
            }
        },
        onSessionEnd: async () => {
            state.active = false;
            if (state.timer) {
                clearInterval(state.timer);
            }
        },
    },
    tools: [],
});

session.on("session.idle", () => {
    state.idle = true;
    void flushPrompt();
});

void initializeSession();
