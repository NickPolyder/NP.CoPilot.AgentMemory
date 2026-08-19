import assert from "node:assert/strict";
import test from "node:test";

import { loadExtension } from "./harness.mjs";

const LOADER_URL =
    "file:///C:/Users/nickpo.copilot/installed-plugins/malformed/.github/extensions/np-agent-memory-inbox/extension.mjs";
const WORKING_DIRECTORY = String.raw`H:\Repos\NP\NP.CoPilot.AgentMemory`;
const FAILURE_WARNING_PREFIX =
    "np-agent-memory inbox notifier check failed: ";
const PROMPT_ON_URGENT_SETTINGS = {
    inboxNotifier: {
        enabled: true,
        executablePath: "np-agent-memory",
        pollIntervalSeconds: 60,
        promptMode: "prompt-on-urgent",
    },
};

function getFailureWarnings(logCalls) {
    return logCalls.filter(
        ({ level, message }) =>
            level === "warning" &&
            message.startsWith(FAILURE_WARNING_PREFIX),
    );
}

function getInfoMessages(logCalls) {
    return logCalls
        .filter(({ level }) => level === "info")
        .map(({ message }) => message);
}

test(
    "initializeSession_Should_InvokeNpAgentMemoryExecutableDirectly_When_LoaderPathDiffers",
    async () => {
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: {},
            workingDirectory: WORKING_DIRECTORY,
        });

        assert.deepEqual(
            outcome.execFileCalls.map(({ command, args }) => ({ command, args })),
            [
                {
                    command: "np-agent-memory",
                    args: [
                        "inbox-summary",
                        "--agent-cwd",
                        WORKING_DIRECTORY,
                    ],
                },
            ],
        );
    },
);

test(
    "initializeSession_Should_LogActionableJsonWarning_When_NpAgentMemoryExecutableIsMissing",
    async () => {
        const errorMessage = "spawn np-agent-memory ENOENT";
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: {},
            workingDirectory: WORKING_DIRECTORY,
            execFileError: {
                code: "ENOENT",
                killed: false,
                message: errorMessage,
            },
        });
        const failureWarnings = getFailureWarnings(outcome.logCalls);
        const payload =
            failureWarnings.length === 1
                ? JSON.parse(
                      failureWarnings[0].message.slice(
                          FAILURE_WARNING_PREFIX.length,
                      ),
                  )
                : null;

        assert.deepEqual(
            {
                hasStdout: payload ? Object.hasOwn(payload, "stdout") : false,
                payload,
                warningCount: failureWarnings.length,
            },
            {
                hasStdout: false,
                payload: {
                    code: "ENOENT",
                    killed: false,
                    message: errorMessage,
                    signal: null,
                    stderr: null,
                },
                warningCount: 1,
            },
        );
    },
);

test(
    "initializeSession_Should_LogSafeBoundedJsonWarning_When_NpAgentMemoryChildProcessFails",
    async () => {
        const stderrLine = `${String.raw`Traceback at C:\Users\NickP\.copilot\installed-plugins\broken\notify.py`}\n`;
        const longStderr = stderrLine.repeat(80);
        const expectedStderr = `${longStderr.slice(0, 4000)}... [truncated]`;
        const errorMessage =
            "Command failed: np-agent-memory inbox-summary";
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: {},
            workingDirectory: WORKING_DIRECTORY,
            execFileError: {
                code: 23,
                killed: true,
                message: errorMessage,
                signal: "SIGTERM",
                stderr: longStderr,
                stdout: '{"messages":[{"id":"secret","body":"do not log"}]}',
            },
        });
        const failureWarnings = getFailureWarnings(outcome.logCalls);
        const payload =
            failureWarnings.length === 1
                ? JSON.parse(
                      failureWarnings[0].message.slice(
                          FAILURE_WARNING_PREFIX.length,
                      ),
                  )
                : null;

        assert.deepEqual(
            {
                hasStdout: payload ? Object.hasOwn(payload, "stdout") : false,
                payload,
                warningCount: failureWarnings.length,
            },
            {
                hasStdout: false,
                payload: {
                    code: 23,
                    killed: true,
                    message: errorMessage,
                    signal: "SIGTERM",
                    stderr: expectedStderr,
                },
                warningCount: 1,
            },
        );
    },
);

test(
    "initializeSession_Should_SendOnePrompt_When_UrgentUnreadMessagesExistAndPromptModeIsPromptOnUrgent",
    async () => {
        // Arrange
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: PROMPT_ON_URGENT_SETTINGS,
            workingDirectory: WORKING_DIRECTORY,
            inboxSummary: {
                unread_count: 1,
                urgent_unread_count: 1,
                messages: [{ id: "urgent-1", priority: "urgent" }],
            },
        });

        // Act
        const actual = {
            intervalCount: outcome.intervalCalls.length,
            sendCalls: outcome.sendCalls.map(({ prompt }) => ({ prompt })),
        };

        // Assert
        assert.deepEqual(actual, {
            intervalCount: 1,
            sendCalls: [
                {
                    prompt:
                        "You have 1 unread inbox message (1 urgent). " +
                        "Read it with np-agent-memory-inbox_check before continuing if it affects current work.",
                },
            ],
        });
    },
);

test(
    "initializeSession_Should_NotSendPrompt_When_UnreadMessagesAreNotUrgentAndPromptModeIsPromptOnUrgent",
    async () => {
        // Arrange
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: PROMPT_ON_URGENT_SETTINGS,
            workingDirectory: WORKING_DIRECTORY,
            inboxSummary: {
                unread_count: 2,
                urgent_unread_count: 0,
                messages: [
                    { id: "normal-1", priority: "normal" },
                    { id: "low-1", priority: "low" },
                ],
            },
        });

        // Act
        const actual = {
            infoMessages: getInfoMessages(outcome.logCalls),
            sendCalls: outcome.sendCalls,
        };

        // Assert
        assert.deepEqual(actual, {
            infoMessages: ["np-agent-memory: 2 unread inbox messages."],
            sendCalls: [],
        });
    },
);
