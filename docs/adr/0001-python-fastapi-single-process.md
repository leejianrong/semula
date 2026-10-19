# ADR-0001: Python and FastAPI in one process on one port

- Status: Accepted
- Date: 2026-10-19
- Deciders: Jian (owner)

## Context

semula must run as one small container, offer an OpenAI-compatible API, an admin API, an MCP endpoint and later a dashboard, and match prompts with lexical and optionally embedding search. The dashboard framework (indah) is Python. The first design chat compared Go and FastAPI.

## Decision

Build semula in Python with FastAPI and Uvicorn. One process serves `/v1` (LLM API), `/api/v1` (admin), `/mcp` and later `/ui` (indah mounted as an ASGI sub-app) on one port, default 8080. Toolchain is `uv`. MCP uses FastMCP.

## Alternatives considered

| Option | Why not |
|--------|---------|
| Go single binary | Tiny image and fast, but embeddings need CGO, FastMCP has no equivalent, and indah is Python, so the dashboard would be a second process in another language. |
| Separate processes for API, MCP and dashboard | More moving parts in a container meant to be one command; shared service layer is simpler in-process. |
| PyTorch / sentence-transformers | Pulls hundreds of MB; ONNX via fastembed is enough and optional. |

## Consequences

Buys one codebase, shared types and one container. Costs a larger image than Go (target under 350 MB without embeddings) and a Python runtime. If the dashboard later moves to Svelte, it becomes another client of `/api/v1`; the service is unaffected.
