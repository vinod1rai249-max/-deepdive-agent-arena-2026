You are checking a set of already-verified claims for the same sub-question for genuine disagreements — not just different wording, but claims that actually conflict (e.g. different figures for the same metric, or opposite assertions about the same fact).

Sub-question: {sub_question}

Verified claims:
{claims}

Identify every pair of claims above that genuinely contradict each other. Do not flag claims that are simply about different things, or that are consistent but phrased differently.

For each contradiction found, give:
- claim_index_a: the number of the first claim
- claim_index_b: the number of the second claim
- explanation: one sentence on what they disagree about

If there are no contradictions, return an empty list.
