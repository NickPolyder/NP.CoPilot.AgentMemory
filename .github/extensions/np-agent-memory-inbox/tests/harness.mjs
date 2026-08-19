import { readFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import vm from "node:vm";

const extensionSourcePath = new URL("../extension.mjs", import.meta.url);
const extensionSource = await readFile(extensionSourcePath, "utf8");

const DEFAULT_MODULE_URL =
    "file:///C:/Users/nickpo.copilot/installed-plugins/broken/.github/extensions/np-agent-memory-inbox/extension.mjs";
const DEFAULT_WORKING_DIRECTORY = "H:/Repos/NP/NP.CoPilot.AgentMemory";

function createSyntheticModule(context, identifier, exportsMap) {
    return new vm.SyntheticModule(
        Object.keys(exportsMap),
        function setExports() {
            for (const [name, value] of Object.entries(exportsMap)) {
                this.setExport(name, value);
            }
        },
        { context, identifier },
    );
}

function createExecFileStub(execFileCalls, inboxSummary) {
    function recordCall(command, args, options) {
        execFileCalls.push({
            command,
            args: [...args],
            options: { ...options },
        });
    }

    function execFile(command, args, options, callback) {
        recordCall(command, args, options);
        callback(null, JSON.stringify(inboxSummary), "");
    }

    execFile[promisify.custom] = async (command, args, options) => {
        recordCall(command, args, options);
        return {
            stdout: JSON.stringify(inboxSummary),
            stderr: "",
        };
    };

    return execFile;
}

async function flushAsync(turns = 4) {
    for (let index = 0; index < turns; index += 1) {
        await new Promise((resolve) => setImmediate(resolve));
    }
}

export function countConfigurationWarnings(logCalls) {
    return logCalls.filter(
        ({ level, message }) =>
            level === "warning" &&
            /pluginroot|plugin root|install-inbox-notifier|absolute path/i.test(
                message,
            ),
    ).length;
}

export async function loadExtension(options = {}) {
    const {
        moduleUrl = DEFAULT_MODULE_URL,
        settings = {
            inboxNotifier: {
                enabled: true,
                pollIntervalSeconds: 60,
                promptMode: "notify",
            },
        },
        workingDirectory = DEFAULT_WORKING_DIRECTORY,
        inboxSummary = {
            unread_count: 0,
            urgent_unread_count: 0,
            messages: [],
        },
        readFileError = null,
    } = options;

    const execFileCalls = [];
    const readFileCalls = [];
    const logCalls = [];
    const sendCalls = [];
    const intervalCalls = [];
    const clearIntervalCalls = [];
    let hooks = null;
    let idleHandler = null;
    let intervalId = 0;

    const fakeSession = {
        log: async (message, { level = "info" } = {}) => {
            logCalls.push({ level, message });
        },
        send: async (payload) => {
            sendCalls.push(payload);
        },
        rpc: {
            metadata: {
                snapshot: async () => ({ workingDirectory }),
            },
        },
        on(eventName, handler) {
            if (eventName === "session.idle") {
                idleHandler = handler;
            }
        },
    };

    const sandbox = {
        clearInterval: (handle) => {
            clearIntervalCalls.push(handle);
        },
        setInterval: (callback, milliseconds) => {
            const handle = { id: ++intervalId };
            intervalCalls.push({ callback, handle, milliseconds });
            return handle;
        },
    };
    sandbox.globalThis = sandbox;

    const context = vm.createContext(sandbox);
    const execFile = createExecFileStub(execFileCalls, inboxSummary);

    const linker = async (specifier) => {
        switch (specifier) {
            case "node:child_process":
                return createSyntheticModule(context, specifier, { execFile });
            case "node:fs/promises":
                return createSyntheticModule(context, specifier, {
                    readFile: async (targetPath, encoding) => {
                        readFileCalls.push({ encoding, targetPath });
                        if (readFileError) {
                            throw readFileError;
                        }
                        return JSON.stringify(settings);
                    },
                });
            case "node:os":
                return createSyntheticModule(context, specifier, {
                    homedir: () => String.raw`C:\Users\NickP`,
                });
            case "node:path":
                return createSyntheticModule(context, specifier, {
                    dirname: path.dirname,
                    isAbsolute: path.isAbsolute,
                    join: path.join,
                    resolve: path.resolve,
                });
            case "node:util":
                return createSyntheticModule(context, specifier, { promisify });
            case "node:url": {
                const { fileURLToPath } = await import("node:url");
                return createSyntheticModule(context, specifier, { fileURLToPath });
            }
            case "@github/copilot-sdk/extension":
                return createSyntheticModule(context, specifier, {
                    joinSession: async ({ hooks: registeredHooks }) => {
                        hooks = registeredHooks;
                        return fakeSession;
                    },
                });
            default:
                throw new Error(`Unsupported import: ${specifier}`);
        }
    };

    const module = new vm.SourceTextModule(extensionSource, {
        context,
        identifier: moduleUrl,
        initializeImportMeta(meta) {
            meta.url = moduleUrl;
        },
    });

    await module.link(linker);
    await module.evaluate();
    await flushAsync();

    return {
        clearIntervalCalls,
        execFileCalls,
        hooks,
        idleHandler,
        intervalCalls,
        logCalls,
        readFileCalls,
        sendCalls,
        async runInterval(index = 0) {
            const interval = intervalCalls[index];
            if (!interval) {
                return false;
            }

            interval.callback();
            await flushAsync();
            return true;
        },
    };
}
