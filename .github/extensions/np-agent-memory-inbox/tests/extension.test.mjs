import assert from "node:assert/strict";
import test from "node:test";

import { countConfigurationWarnings, loadExtension } from "./harness.mjs";

const CONFIGURED_PLUGIN_ROOT =
    String.raw`C:\Users\NickP\.copilot\installed-plugins\_direct\NickPolyder--NP.CoPilot.AgentMemory`;
const LOADER_URL =
    "file:///C:/Users/nickpo.copilot/installed-plugins/malformed/.github/extensions/np-agent-memory-inbox/extension.mjs";
const WORKING_DIRECTORY = String.raw`H:\Repos\NP\NP.CoPilot.AgentMemory`;

function createSettings(pluginRoot) {
    return {
        inboxNotifier: {
            enabled: true,
            pollIntervalSeconds: 60,
            promptMode: "notify",
            ...(pluginRoot === undefined ? {} : { pluginRoot }),
        },
    };
}

test(
    "initializeSession_Should_UseConfiguredPluginRoot_When_LoaderPathDiffers",
    async () => {
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: createSettings(CONFIGURED_PLUGIN_ROOT),
            workingDirectory: WORKING_DIRECTORY,
        });

        assert.deepEqual(
            outcome.execFileCalls.map(({ command, args }) => ({ command, args })),
            [
                {
                    command: "uvx",
                    args: [
                        "--from",
                        CONFIGURED_PLUGIN_ROOT,
                        "np-agent-memory",
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
    "initializeSession_Should_LogWarningAndSkipLaunch_When_PluginRootMissing",
    async () => {
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: createSettings(undefined),
            workingDirectory: WORKING_DIRECTORY,
        });
        await outcome.runInterval();

        assert.deepEqual(
            {
                configurationWarningCount: countConfigurationWarnings(
                    outcome.logCalls,
                ),
                execFileCallCount: outcome.execFileCalls.length,
            },
            {
                configurationWarningCount: 1,
                execFileCallCount: 0,
            },
        );
    },
);

test(
    "initializeSession_Should_LogWarningAndSkipLaunch_When_PluginRootIsRelative",
    async () => {
        const outcome = await loadExtension({
            moduleUrl: LOADER_URL,
            settings: createSettings(".\\relative-plugin-root"),
            workingDirectory: WORKING_DIRECTORY,
        });
        await outcome.runInterval();

        assert.deepEqual(
            {
                configurationWarningCount: countConfigurationWarnings(
                    outcome.logCalls,
                ),
                execFileCallCount: outcome.execFileCalls.length,
            },
            {
                configurationWarningCount: 1,
                execFileCallCount: 0,
            },
        );
    },
);
