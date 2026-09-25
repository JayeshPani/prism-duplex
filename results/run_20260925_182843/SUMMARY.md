# Run run_20260925_182843

judge: none 

**Strict pass rate: 50.0%** (5/10)

- by_difficulty: {"medium": 1.0, "easy": 0.444}
- by_domain: {"ecommerce_support": 0.5}
- by_disfluency_feature: {"SELF_CORRECTION": 1.0, "FILLER": 0.0, "HESITATION": 0.0, "PAUSE": 0.333, "FALSE_START": 1.0}
- by_state_rollback: {"with_rollback": 1.0, "without_rollback": 0.444}
- failure_breakdown: {"wrong_tools": 1, "wrong_arguments": 4}

## Tool-call evaluation

- benchmark_name: "In-the-Wild Speech & Multi-Step Tool Calling"
- evaluated_at: "2026-09-25T18:38:23.995353"
- total_scenarios: 10
- turn_taking: {"total": 10, "turn_taken": 10, "no_response": 0, "turn_take_rate": 1.0}
- by_metric: {"tool_selection_acc": 0.967, "argument_acc": 0.6, "response_qual": null, "tool_selection_acc_all": 0.967, "argument_acc_all": 0.6, "note": "*_all includes no-response samples (scored 0); default metrics are turn-taken only"}
- latency: {"total_samples": 10, "interruption_count": 1, "interruption_rate": 0.1, "avg_response_latency_s": 6.249, "std_response_latency_s": 4.505, "min_latency_s": 2.48, "max_latency_s": 17.52, "note": "avg/std/min/max exclude interruption samples; computed on turn-taken samples only"}
- by_domain: {"ecommerce_support": {"tool_selection_acc": 0.967, "argument_acc": 0.6}}
- by_difficulty: {"medium": {"tool_selection_acc": 1.0, "argument_acc": 1.0}, "easy": {"tool_selection_acc": 0.963, "argument_acc": 0.556}}
- by_domain_turn_taken: {"ecommerce_support": {"tool_selection_acc": 0.967, "argument_acc": 0.6}}
- by_difficulty_turn_taken: {"medium": {"tool_selection_acc": 1.0, "argument_acc": 1.0}, "easy": {"tool_selection_acc": 0.963, "argument_acc": 0.556}}
