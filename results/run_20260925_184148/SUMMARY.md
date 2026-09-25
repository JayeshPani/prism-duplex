# Run run_20260925_184148

judge: none 

**Strict pass rate: 60.0%** (6/10)

- by_difficulty: {"medium": 1.0, "easy": 0.556}
- by_domain: {"ecommerce_support": 0.6}
- by_disfluency_feature: {"SELF_CORRECTION": 1.0, "FILLER": 1.0, "HESITATION": 1.0, "PAUSE": 0.333, "FALSE_START": 1.0}
- by_state_rollback: {"with_rollback": 1.0, "without_rollback": 0.556}
- failure_breakdown: {"wrong_tools": 0, "wrong_arguments": 4}

## Tool-call evaluation

- benchmark_name: "In-the-Wild Speech & Multi-Step Tool Calling"
- evaluated_at: "2026-09-25T18:51:52.249786"
- total_scenarios: 10
- turn_taking: {"total": 10, "turn_taken": 9, "no_response": 1, "turn_take_rate": 0.9}
- by_metric: {"tool_selection_acc": 1.0, "argument_acc": 0.667, "response_qual": null, "tool_selection_acc_all": 1.0, "argument_acc_all": 0.6, "note": "*_all includes no-response samples (scored 0); default metrics are turn-taken only"}
- latency: {"total_samples": 9, "interruption_count": 1, "interruption_rate": 0.111, "avg_response_latency_s": 6.07, "std_response_latency_s": 5.395, "min_latency_s": 2.56, "max_latency_s": 19.04, "note": "avg/std/min/max exclude interruption samples; computed on turn-taken samples only"}
- by_domain: {"ecommerce_support": {"tool_selection_acc": 1.0, "argument_acc": 0.6}}
- by_difficulty: {"medium": {"tool_selection_acc": 1.0, "argument_acc": 1.0}, "easy": {"tool_selection_acc": 1.0, "argument_acc": 0.556}}
- by_domain_turn_taken: {"ecommerce_support": {"tool_selection_acc": 1.0, "argument_acc": 0.667}}
- by_difficulty_turn_taken: {"medium": {"tool_selection_acc": 1.0, "argument_acc": 1.0}, "easy": {"tool_selection_acc": 1.0, "argument_acc": 0.625}}
