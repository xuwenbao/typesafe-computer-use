# Writer endpoints

The writer types text, proposes URLs, and reads the screen whenever the classifier stops. By
default it speaks OpenAI's Chat Completions API at `https://api.openai.com/v1`, with `gpt-6.1-terra`
for writing and `gpt-6.1-sol` for answers. The key is `OPENAI_API_KEY`. These variables change the
model or send it to another endpoint. `config.settings()` is the only way the program reads them.

| variable | required | purpose |
|---|---|---|
| `CLICKER_WRITER_BASE_URL` | no | another endpoint. Unset is `https://api.openai.com/v1`. When the API is `anthropic`, unset leaves the endpoint to the Anthropic SDK |
| `CLICKER_WRITER_API_KEY` | no | the key for an endpoint you name. Unset on `api.openai.com` uses `OPENAI_API_KEY`. An endpoint that checks no key can leave both empty |
| `CLICKER_WRITER_API` | no | what that endpoint speaks: `openai` (the default) or `anthropic` |
| `CLICKER_WRITER_MODEL` | no | types text and proposes URLs; defaults to `gpt-6.1-terra` |
| `CLICKER_ANSWER_MODEL` | no | reads the screen whenever the classifier stops; defaults to `gpt-6.1-sol` |
| `CLICKER_DRIVER` | no | a stronger model takes over once the classifier stops short of the goal for good (see [the takeover](how-a-step-works.md#the-takeover)); defaults to `true`, and `false` leaves the stop where it is |
| `CLICKER_DRIVER_STEPS` | no | the most actions one takeover takes; defaults to `20`, and does not come out of `--steps` |
| `CLICKER_DRIVER_MODEL` | no | the model that takes over; defaults to the answer model, and uses its reasoning effort |
| `CLICKER_WRITER_VISION` | no | `false` for an answer model that reads text only; defaults to `true` |
| `CLICKER_WRITER_REASONING` | no | the reasoning effort the writer's calls ask an OpenAI-API model for, such as `none`; unset leaves it to the model |
| `CLICKER_WRITER_TIMEOUT` | no | seconds one writer request may take before it is given up; defaults to `120`, and a request is retried once |
| `CLICKER_ANSWER_REASONING` | no | the same for the answer model, such as `low` |

## Other models

Point `CLICKER_WRITER_BASE_URL` at any endpoint that speaks OpenAI's Chat Completions API, which is
the default, or set `CLICKER_WRITER_API=anthropic` for the Anthropic Messages API: LM Studio,
Ollama, vLLM, a LiteLLM proxy, DeepSeek. Name the models it serves. The full request URL works as
well as the root; for the OpenAI API keep the `/v1`. Such an endpoint may ignore structured-output
parameters, so the schema is also spelled out in the prompt, and code fences or a sentence around
the JSON are tolerated. On the OpenAI API a `json_schema` response format is asked for first, then
`json_object`, then none, stepping down only when the endpoint refuses one. Thinking is turned off,
since a model that thinks by default spends the writer's small token budgets on it and returns no
text. The answer model reads a screenshot; for a model that reads text only, set
`CLICKER_WRITER_VISION=false` and it gets the screen's text alone. A reply that cannot be read
refuses the step it was for, and the run goes on.

`OPENAI_API_KEY` goes only to `api.openai.com`. A different `CLICKER_WRITER_BASE_URL` gets
`CLICKER_WRITER_API_KEY` and never `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. On the Anthropic API the
key is sent in both the `x-api-key` and `Authorization` headers, since proxies differ. Leave it
empty for an endpoint that checks no key.

## Examples

```
# LM Studio, either of its two APIs
CLICKER_WRITER_BASE_URL=http://localhost:1234
CLICKER_WRITER_MODEL=qwen3.8-flash-next
CLICKER_ANSWER_MODEL=qwen3.8-flash-next

# any OpenAI-compatible server
CLICKER_WRITER_API=openai
CLICKER_WRITER_BASE_URL=https://api.deepseek.com/v1
CLICKER_WRITER_API_KEY=sk-...
CLICKER_WRITER_MODEL=deepseek-v4.1-flash
CLICKER_ANSWER_MODEL=deepseek-v4.1-flash
CLICKER_WRITER_VISION=false
```
