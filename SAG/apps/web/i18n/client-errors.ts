import type { AppLocale } from "./config";
import { readClientLocale } from "./client";

interface ClientErrorValues {
  event?: string;
  status?: number;
  heading?: string;
  index?: number;
  label?: string;
}

const messages = {
  "en-US": {
    invalidSearchStream: "The search service returned an unreadable data stream",
    searchPrepareTimeout: "Search preparation timed out. Try again.",
    searchIdleTimeout: "The search stream stopped responding. Try again.",
    cancelled: "Request cancelled",
    network: "Network error. Try again shortly.",
    searchFailed: "Search failed",
    invalidResult: "The search result event order or format is invalid",
    summaryBeforeResult: "A search summary arrived before the search result",
    invalidSummaryDelta: "The search summary update format is invalid",
    invalidCompleted: "The search completion event order or format is invalid",
    unknownSearchEvent: ({ event }: ClientErrorValues) => `Unknown search stream event: ${event || "(empty)"}`,
    searchIncomplete: "The search connection ended early. Try again.",
    requestTimeout: "The request timed out. Check your connection and try again.",
    octxExportStartBusy: "The export check is temporarily busy. Try again shortly.",
    requestFailed: "Request failed",
    uploadFailed: "Upload failed",
    uploadNetwork: "A network error interrupted the upload",
    newConversation: "New conversation",
    missingSource: "The knowledge node is missing its source context",
    conversationBusy: "An answer is already being generated. Wait for it to finish or stop it first.",
    historyLoadFailed: "Failed to load conversation",
    queryOrAttachmentRequired: "Provide a question or at least one attachment",
    conversationCreateFailed: "Failed to create conversation",
    connectionInterrupted: "Connection interrupted",
    userRejected: "User rejected the action",
    messageMissing: "Message not found",
    originalQuestionMissing: "Original question not found",
    conversationNotCreated: "The conversation has not been created yet",
    deleteMessageUnsupported: "The current connection does not support deleting messages",
    threadAlreadyBound: "The conversation is already bound to another runtime session",
    tool: "Tool",
    toolFailed: "Tool execution failed",
    stopped: "Stopped",
    generationFailed: "Generation failed",
    approvalExpired: "This tool approval is no longer valid",
    approvalFailed: "Failed to process tool approval",
    sessionMissing: ({ event }: ClientErrorValues) => `Conversation runtime session not found: ${event || "-"}`,
    unreadableAgentEvent: "The server returned an unreadable Agent event",
    agentEventNotObject: "The Agent event must be an object",
    agentEventMissingFields: "The Agent event is missing required fields",
    agentEventTypeMismatch: "The SSE event name does not match the Agent event type",
    eventAfterTerminal: "An extra event arrived after the terminal event",
    missingTerminalEvent: "The connection ended before an Agent terminal event arrived",
    petThinking: "Thinking through the next step",
    petSearching: "Searching the knowledge pack",
    petWorking: "Using tools",
    petAnswering: "Composing the answer",
    petComplete: "Answer complete. Take a look.",
    petFailed: "That did not finish successfully",
    citationSection: ({ heading }: ClientErrorValues) => `Section: ${heading || "-"}`,
    externalSource: ({ index }: ClientErrorValues) => `External source ${index ?? "-"}`,
    knowledgeSource: ({ index }: ClientErrorValues) => `Knowledge source ${index ?? "-"}`,
    askEntity: ({ label }: ClientErrorValues) => `Organize the key facts, related events, and timeline around “${label || "-"}”, citing the supporting knowledge sources.`,
    askEvent: ({ label }: ClientErrorValues) => `Explain the background, key entities, and later relationships of “${label || "-"}”, citing the supporting knowledge sources.`,
    serverNotFound: "The requested resource was not found",
    serverConflict: "This action conflicts with the current resource state",
    serverInvalidRequest: "The request is invalid. Check it and try again.",
    serverUnauthorized: "Your session has expired. Sign in again.",
    serverForbidden: "You do not have permission to perform this action",
    serverConfiguration: "The service is not fully configured. Complete the required settings first.",
    serverUpstream: "An upstream service failed to process the request. Try again shortly.",
    serverUnavailable: "The service is temporarily unavailable. Try again shortly.",
    serverUnexpected: "The service encountered an unexpected error. Try again shortly.",
    serverRequestFailed: "The request could not be processed. Try again shortly.",
  },
};

const localizedMessages = {
  ...messages,
  "vi-VN": messages["en-US"],
} as Record<AppLocale, Record<string, string | ((values: ClientErrorValues) => string)>>;

export type ClientErrorKey = keyof (typeof messages)["en-US"];

export function clientErrorMessage(
  key: ClientErrorKey,
  values: ClientErrorValues = {},
  locale = readClientLocale(),
): string {
  const message = localizedMessages[locale][key];
  return typeof message === "function" ? message(values) : message;
}

/**
 * The API predates UI localization and can still return fixed Chinese error
 * messages. Preserve useful messages that already match the active locale,
 * but provide a stable English fallback from the machine-readable code/status
 * instead of leaking untranslated implementation text into the interface.
 */
export function serverErrorMessage(
  code: string | undefined,
  message: string,
  status = 0,
  locale = readClientLocale(),
): string {
  if (!["en-US", "vi-VN"].includes(locale) || !/\p{Script=Han}/u.test(message)) return message;

  const normalizedCode = (code ?? "").toLowerCase();
  let key: ClientErrorKey = "serverRequestFailed";
  if (normalizedCode.includes("not_found") || status === 404) key = "serverNotFound";
  else if (normalizedCode.includes("conflict") || status === 409) key = "serverConflict";
  else if (normalizedCode.includes("unauthorized") || status === 401) key = "serverUnauthorized";
  else if (normalizedCode.includes("forbidden") || status === 403) key = "serverForbidden";
  else if (normalizedCode.includes("configuration")) key = "serverConfiguration";
  else if (normalizedCode.includes("upstream") || status === 502) key = "serverUpstream";
  else if (
    normalizedCode.includes("unavailable")
    || normalizedCode.includes("timeout")
    || status === 429
    || status === 503
    || status === 504
  ) key = "serverUnavailable";
  else if (
    normalizedCode.includes("validation")
    || normalizedCode.startsWith("invalid_")
    || status === 400
    || status === 422
  ) key = "serverInvalidRequest";
  else if (status >= 500) key = "serverUnexpected";

  return clientErrorMessage(key, {}, locale);
}
