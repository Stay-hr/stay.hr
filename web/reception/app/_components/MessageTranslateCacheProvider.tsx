"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { MessageTranslateSession } from "@/lib/messageTranslate";

const MessageTranslateCacheContext = createContext<MessageTranslateSession | null>(null);

export function MessageTranslateCacheProvider({ children }: { children: ReactNode }) {
  const [session] = useState(() => new MessageTranslateSession());

  useEffect(() => {
    return () => {
      session.abortAll();
    };
  }, [session]);

  return (
    <MessageTranslateCacheContext.Provider value={session}>
      {children}
    </MessageTranslateCacheContext.Provider>
  );
}

export function useMessageTranslateCache(): MessageTranslateSession {
  const session = useContext(MessageTranslateCacheContext);
  if (!session) {
    throw new Error("useMessageTranslateCache must be used within MessageTranslateCacheProvider");
  }
  return session;
}
