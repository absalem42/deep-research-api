// Directory: deep-research/frontend/contexts/ResearchContext.tsx
/**
 * Research state shared across tabs.
 *
 * Provider API keys used to live here, in localStorage under `dra_api_keys`,
 * and were POSTed to the backend with every request. That is gone: any XSS on
 * the page could read them, and it forced every user to hold their own paid
 * OpenAI/Anthropic key just to use the app.
 *
 * Provider credentials now live only in the backend's environment. The browser
 * talks to a same-origin route that attaches a scoped service key server-side.
 */

'use client'

import { createContext, useContext, useState, ReactNode } from 'react'

export interface StreamingEvent {
  type: string
  stage?: string
  content?: string
  timestamp: string
  research_id?: string
  model?: string
  error?: string
  node_name?: string
  node_count?: number
  duration?: number
  data?: Record<string, unknown>
}

export interface ResearchMessage {
  id: string
  type: 'user' | 'assistant' | 'system'
  content: string
  timestamp: string
  model?: string
  stage?: string
  isStreaming?: boolean
}

interface ResearchState {
  messages: ResearchMessage[]
  streamingEvents: StreamingEvent[]
  currentStage: string
  isStreaming: boolean
  /** Provider id (anthropic | openai | moonshot | openrouter | ...). */
  selectedModel: string
  setMessages: (messages: ResearchMessage[] | ((prev: ResearchMessage[]) => ResearchMessage[])) => void
  setStreamingEvents: (events: StreamingEvent[] | ((prev: StreamingEvent[]) => StreamingEvent[])) => void
  setCurrentStage: (stage: string) => void
  setIsStreaming: (streaming: boolean) => void
  setSelectedModel: (model: string) => void
}

const ResearchContext = createContext<ResearchState | undefined>(undefined)

export const useResearchState = () => {
  const context = useContext(ResearchContext)
  if (!context) {
    throw new Error('useResearchState must be used within ResearchProvider')
  }
  return context
}

export const ResearchProvider = ({ children }: { children: ReactNode }) => {
  const [messages, setMessages] = useState<ResearchMessage[]>([])
  const [streamingEvents, setStreamingEvents] = useState<StreamingEvent[]>([])
  const [currentStage, setCurrentStage] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [selectedModel, setSelectedModel] = useState('anthropic')

  const researchState: ResearchState = {
    messages,
    streamingEvents,
    currentStage,
    isStreaming,
    selectedModel,
    setMessages,
    setStreamingEvents,
    setCurrentStage,
    setIsStreaming,
    setSelectedModel
  }

  return (
    <ResearchContext.Provider value={researchState}>
      {children}
    </ResearchContext.Provider>
  )
}
