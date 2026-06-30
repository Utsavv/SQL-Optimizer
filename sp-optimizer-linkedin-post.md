Most SQL tuning advice optimizes for one execution plan. Real workloads don't run one plan. They run the same proc against a narrow date range, a wide one, and an empty one, and quietly fall apart on two of the three.

So I built sp-optimizer: an AI agent that tunes SQL Server stored procedures as a closed feedback loop, not a one-shot suggestion.

Here is the architecture, and the three ideas I am most proud of.

The loop
Point it at any stored procedure. It reads the signature from sys.parameters, then derives a realistic workload from the proc's own data (narrow, medium, wide, and empty parameter windows, the exact spread that exposes parameter sniffing). Then it cycles:

discover -> capture plans -> analyze plan XML -> decide a change -> apply to a sandbox copy -> re-verify across the same workload -> repeat.

It stops when the majority of parameter combinations (default 80%) land on a good plan with no regressions, not when one lucky compile looks fast. Loop engineering, not prompt-and-pray.

Self-learning via a decision log
The decision step grounds every proposed change in official Microsoft Learn guidance. But fetching docs is the most expensive part of the run. So the agent keeps a decision-log.md: every lookup it has ever made, keyed by the finding it answered. Next run, it greps the cache before it pays for a fresh fetch. A cache hit costs a grep. A miss costs a full search-and-fetch, and then writes a new entry so it never pays for that question twice. The system gets cheaper and faster the more procs you throw at it.

Token optimization as a first-class concern
The deterministic work (plan capture, plan scoring, bottleneck detection) uses zero tokens. The LLM is only invoked at one step, to propose a single smallest-safe change. Combined with the decision-log cache, token spend stays flat even as runs accumulate.

A few more things that matter:
- Pluggable LLM backend. Local Ollama, OpenAI, Anthropic, Gemini, Azure, Bedrock, swap via one env var, no code change. Or replay agent decisions from JSON with no API key at all.
- Safety is non-negotiable. It never touches the live proc. Every change goes to a sandbox copy with a diff and a rollback script. No index lands on production without surfacing its write cost first.
- Evidence is the contract. Every plan and IO stat at every step is written to disk and linked from a self-contained HTML report. Nothing it reasoned over lives only in memory.

Procedure-agnostic by design. No allow-list, no per-proc setup. Name the proc, pass a connection string, run the loop.

The interesting shift here is treating an LLM agent like an engineering system: deterministic where it can be, cached where it repeats itself, and bounded by safety rules it cannot talk its way out of.

Code: https://github.com/Utsavv/SQL-Optimizer

What would you want an agent like this to learn next?

#SQLServer #AI #AgenticAI #DatabasePerformance #LLM #SoftwareEngineering
