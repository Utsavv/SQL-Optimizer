Most SQL tuning fixes one execution plan. Real workloads run the same proc across narrow, wide, and empty parameter ranges, and quietly fall apart on two of them.

So I built sp-optimizer: an AI agent that tunes SQL Server stored procedures as a closed loop, not a one-shot guess.

How it works: point it at any proc. It reads the signature, derives a realistic workload from the proc's own data, then cycles capture plan -> analyze -> propose one smallest-safe change -> apply to a sandbox copy -> re-verify. It stops only when the majority of parameter combinations land on a good plan with no regressions.

Three ideas I am proud of:

1. Loop engineering, not prompt-and-pray. Termination is driven by workload-wide scores, not one lucky compile.

2. Self-learning via a decision log. Every Microsoft Learn lookup is cached in decision-log.md, keyed by the finding it answered. A cache hit costs a grep; a miss writes a new entry so it never pays for that question twice. It gets cheaper the more you run it.

3. Token optimization by design. Plan capture and scoring are fully deterministic and use zero tokens. The LLM runs at exactly one step. Spend stays flat as runs accumulate.

Plus: pluggable LLM backend (Ollama, OpenAI, Anthropic, Gemini, Azure, Bedrock via one env var), never touches the live proc, and every plan it reasoned over is written to disk and linked from an HTML report.

Code: https://github.com/Utsavv/SQL-Optimizer

What would you want an agent like this to learn next?

#SQLServer #AI #AgenticAI #DatabasePerformance #LLM
