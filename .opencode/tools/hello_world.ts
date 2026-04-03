import { tool } from "@opencode-ai/plugin"
import fs from "fs"
import path from "path"

export default tool({
  description:
    "A simple hello world diagnostic tool. Use this when the user asks to " +
    "perform a hello world test. Accepts an optional greeting message.",
  args: {
    message: tool.schema
      .string()
      .optional()
      .describe("An optional greeting message to include in the response"),
  },
  async execute(args, context) {
    const timestamp = new Date().toISOString()
    const logEntry = {
      timestamp,
      tool: "hello_world",
      args,
      context: {
        agent: context.agent,
        sessionID: context.sessionID,
        messageID: context.messageID,
        directory: context.directory,
        worktree: context.worktree,
      },
    }

    const logDir = path.join(context.worktree || context.directory, "logs")
    fs.mkdirSync(logDir, { recursive: true })
    const logFile = path.join(logDir, "hello_world.jsonl")
    fs.appendFileSync(logFile, JSON.stringify(logEntry) + "\n")

    const greeting = args.message || "Hello, World!"
    return `${greeting} (logged at ${timestamp} to logs/hello_world.jsonl)`
  },
})
