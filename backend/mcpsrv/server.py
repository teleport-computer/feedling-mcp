"""The FastMCP instance; tool modules attach to it at import."""

from fastmcp import FastMCP

mcp = FastMCP(
    name="Feedling",
    instructions=(
        "Feedling gives your Agent a body on iOS. "
        "Use these tools to push to Dynamic Island, read the user's screen, "
        "chat with the user, manage the identity card, and tend the memory garden. "
        "Start with feedling_bootstrap on first connection."
    ),
)
