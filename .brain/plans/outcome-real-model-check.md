# Check the structured Outcome against a real model

Status: open.

0.2.0 passes `output_model_schema=Outcome` to the browser-use Agent
(`src/swarm_persona/agent.py:67`, `:171`) so the done action carries `verdict` and `worked_well`.
`read_outcome` handles a structured reply, plain text and nothing, but only unit tests with canned
strings cover it (`tests/test_verdicts.py`). No run against a real model has confirmed that:

- the default Bedrock model fills the schema through the forced tool in `src/swarm_persona/llm.py`;
- `history.final_result()` is the JSON of the schema and not a wrapper;
- `worked_well` arrives as a list, and the verdict still starts with ACHIEVED: or GAVE UP:.

Do: one small run (one journey, one persona, low max_steps) on the default provider and on
anthropic, then read `evidence.json` sessions. Record what came back as a fact. Done when both
providers are confirmed or `read_outcome` handles what they actually send.
