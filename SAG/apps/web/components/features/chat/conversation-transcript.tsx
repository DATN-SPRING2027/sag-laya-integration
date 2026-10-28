"use client";

import * as React from "react";
import { useTranslations } from "next-intl";

import type { Citation, SessionCitation } from "@/lib/types";
import { cn } from "@/lib/utils";
import { MarkdownContent } from "@/components/features/markdown-content";
import {
  AgentActivityTimeline,
  type AgentActivityMatch,
  type AgentActivityStep,
} from "@/components/features/chat/agent-activity-timeline";

export interface ConversationTranscriptMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  citations?: Citation[];
  steps?: AgentActivityStep[];
  errorMessage?: string;
}

export interface ConversationLiveActivity {
  messageId: string;
  streaming: boolean;
  steps: readonly AgentActivityStep[];
  collapsed?: boolean;
  onToggle?: () => void;
}

export interface ConversationMessageRenderContext<
  Message extends ConversationTranscriptMessage = ConversationTranscriptMessage,
> {
  message: Message;
  index: number;
  previousUser?: Message;
}

export interface ConversationMessageItemProps {
  message: ConversationTranscriptMessage;
  index: number;
  previousUser?: ConversationTranscriptMessage;
  streaming?: boolean;
  highlighted?: boolean;
  sessionCitations?: SessionCitation[];
  activity?: Omit<ConversationLiveActivity, "messageId" | "streaming">;
  assistantAvatar?: React.ReactNode;
  onCitationClick?: (
    citation: Citation,
    message: ConversationTranscriptMessage,
  ) => void;
  onSessionCitationClick?: (
    citation: SessionCitation,
    message: ConversationTranscriptMessage,
  ) => void;
  onToolMatchClick?: (
    match: AgentActivityMatch,
    step: AgentActivityStep,
    message: ConversationTranscriptMessage,
  ) => void;
  onSessionContextClick?: (
    step: AgentActivityStep,
    message: ConversationTranscriptMessage,
  ) => void;
  renderUserAttachments?: (message: ConversationTranscriptMessage) => React.ReactNode;
  renderAssistantFooter?: (
    context: ConversationMessageRenderContext,
  ) => React.ReactNode;
}

export interface ConversationTranscriptProps<
  Message extends ConversationTranscriptMessage = ConversationTranscriptMessage,
> {
  messages: readonly Message[];
  live?: ConversationLiveActivity;
  highlightedMessageId?: string | null;
  sessionCitationsByMessageId?: ReadonlyMap<string, SessionCitation[]>;
  assistantAvatar?: React.ReactNode;
  empty?: React.ReactNode;
  onCitationClick?: (citation: Citation, message: Message) => void;
  onSessionCitationClick?: (citation: SessionCitation, message: Message) => void;
  onToolMatchClick?: (
    match: AgentActivityMatch,
    step: AgentActivityStep,
    message: Message,
  ) => void;
  onSessionContextClick?: (step: AgentActivityStep, message: Message) => void;
  renderUserAttachments?: (message: Message) => React.ReactNode;
  renderAssistantFooter?: (
    context: ConversationMessageRenderContext<Message>,
  ) => React.ReactNode;
  className?: string;
}

export const ConversationMessageItem = React.memo(function ConversationMessageItem({
  message,
  index,
  previousUser,
  streaming = false,
  highlighted = false,
  sessionCitations,
  activity,
  assistantAvatar,
  onCitationClick,
  onSessionCitationClick,
  onToolMatchClick,
  onSessionContextClick,
  renderUserAttachments,
  renderAssistantFooter,
}: ConversationMessageItemProps) {
  const t = useTranslations("Conversation");
  const handleCitationClick = React.useCallback(
    (citation: Citation) => onCitationClick?.(citation, message),
    [message, onCitationClick],
  );
  const handleSessionCitationClick = React.useCallback(
    (citation: SessionCitation) => onSessionCitationClick?.(citation, message),
    [message, onSessionCitationClick],
  );
  const handleToolMatchClick = React.useCallback(
    (match: AgentActivityMatch, step: AgentActivityStep) =>
      onToolMatchClick?.(match, step, message),
    [message, onToolMatchClick],
  );
  const handleSessionContextClick = React.useCallback(
    (step: AgentActivityStep) => onSessionContextClick?.(step, message),
    [message, onSessionContextClick],
  );

  const highlightClasses = highlighted
    ? "rounded-xl bg-primary/[0.06] ring-2 ring-primary/75 ring-offset-4 ring-offset-background transition-all duration-300 motion-reduce:transition-none"
    : "";

  if (message.role === "user") {
    return (
      <div
        data-message-id={message.id}
        className={cn("scroll-mt-6 flex flex-col items-end gap-1.5", highlightClasses)}
      >
        {renderUserAttachments?.(message)}
        {message.content && (
          <div className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
            {message.content}
          </div>
        )}
      </div>
    );
  }

  const steps = activity?.steps ?? message.steps ?? [];
  const waiting = streaming && !message.content;
  const showFooter = !streaming && (message.content || message.errorMessage);
  const footer = showFooter
    ? renderAssistantFooter?.({ message, index, previousUser })
    : null;

  return (
    <div
      data-message-id={message.id}
      className={cn("group/msg scroll-mt-6 flex gap-3", highlightClasses)}
    >
      {assistantAvatar}
      <div className="min-w-0 flex-1">
        {steps.length > 0 && (
          <AgentActivityTimeline
            steps={steps}
            collapsed={activity?.collapsed}
            onToggle={activity?.onToggle}
            onMatchClick={handleToolMatchClick}
            onSessionContextClick={
              onSessionContextClick ? handleSessionContextClick : undefined
            }
          />
        )}

        {waiting && steps.length === 0 ? (
          <div className="flex items-center gap-1.5 py-1 text-sm">
            <span className="size-1.5 animate-blink rounded-full bg-primary" />
            <span className="text-shimmer">{t("thinking")}</span>
          </div>
        ) : message.content ? (
          <MarkdownContent
            content={message.content}
            citations={message.citations}
            sessionCitations={sessionCitations}
            onCitationClick={handleCitationClick}
            onSessionCitationClick={
              onSessionCitationClick ? handleSessionCitationClick : undefined
            }
            streaming={streaming}
          />
        ) : null}

        {!streaming && message.errorMessage ? (
          <div
            role="alert"
            className={cn(
              "mt-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive",
              message.content ? "" : "mt-0",
            )}
          >
            {message.errorMessage}
          </div>
        ) : null}

        {footer}
      </div>
    </div>
  );
});

export function ConversationTranscript<
  Message extends ConversationTranscriptMessage,
>({
  messages,
  live,
  highlightedMessageId,
  sessionCitationsByMessageId,
  assistantAvatar,
  empty,
  onCitationClick,
  onSessionCitationClick,
  onToolMatchClick,
  onSessionContextClick,
  renderUserAttachments,
  renderAssistantFooter,
  className,
}: ConversationTranscriptProps<Message>) {
  const citationHandler = onCitationClick as ConversationMessageItemProps["onCitationClick"];
  const sessionCitationHandler =
    onSessionCitationClick as ConversationMessageItemProps["onSessionCitationClick"];
  const toolMatchHandler = onToolMatchClick as ConversationMessageItemProps["onToolMatchClick"];
  const sessionContextHandler =
    onSessionContextClick as ConversationMessageItemProps["onSessionContextClick"];
  const attachmentsRenderer = renderUserAttachments as ConversationMessageItemProps["renderUserAttachments"];
  const footerRenderer = renderAssistantFooter as ConversationMessageItemProps["renderAssistantFooter"];

  if (messages.length === 0) {
    return <div className={className}>{empty ?? null}</div>;
  }

  let previousUser: Message | undefined;
  return (
    <div className={cn("flex flex-col gap-6", className)}>
      {messages.map((message, index) => {
        const precedingUser = previousUser;
        if (message.role === "user") previousUser = message;
        const currentActivity = live?.messageId === message.id
          ? {
              steps: live.steps,
              collapsed: live.collapsed,
              onToggle: live.onToggle,
            }
          : undefined;

        return (
          <ConversationMessageItem
            key={message.id}
            message={message}
            index={index}
            previousUser={precedingUser}
            streaming={live?.messageId === message.id && live.streaming}
            highlighted={highlightedMessageId === message.id}
            sessionCitations={sessionCitationsByMessageId?.get(message.id)}
            activity={currentActivity}
            assistantAvatar={assistantAvatar}
            onCitationClick={citationHandler}
            onSessionCitationClick={sessionCitationHandler}
            onToolMatchClick={toolMatchHandler}
            onSessionContextClick={sessionContextHandler}
            renderUserAttachments={attachmentsRenderer}
            renderAssistantFooter={footerRenderer}
          />
        );
      })}
    </div>
  );
}
