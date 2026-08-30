# Tier-1 Pass-2 replay -- candidate `current`

Run id: `tier1-current-ddc8fb442281`
Model: `bedrock:us.anthropic.claude-sonnet-4-6`
Sample: 60 of 60 requested (seed 0)
Ledger cost: $6.72

## Per-question agreement (binary)

- `absence_confirmed_in_context`: raw=0.7347560975609756 kappa=0.5820493291932743 n=328 no_answer=20
- `asserted_capability_confirmed`: raw=0.7957317073170732 kappa=0.45665826039657814 n=328 no_answer=20
- `cited_reference_accurate`: raw=0.5365853658536586 kappa=0.2705352178620548 n=328 no_answer=20
- `claims_absence`: raw=0.7286585365853658 kappa=0.4818785275638068 n=328 no_answer=20
- `committed_work_relies_on_unbacked_claim`: raw=0.8109756097560976 kappa=0.5185606060606063 n=328 no_answer=20
- `current_state_satisfies_plan_goal`: raw=0.7957317073170732 kappa=0.3002610966057441 n=328 no_answer=20
- `evidence_entails_finding`: raw=0.6920731707317073 kappa=0.3807271707636226 n=328 no_answer=20
- `impact_follows_necessarily`: raw=0.774390243902439 kappa=0.5116494306064142 n=328 no_answer=20
- `is_verifiable`: raw=0.9634146341463414 kappa=0.5566569047082675 n=328 no_answer=20
- `no_existing_mitigation`: raw=0.5792682926829268 kappa=0.2574437718391652 n=328 no_answer=20
- `no_viable_alternative_explanation`: raw=0.4969512195121951 kappa=0.03649635036496338 n=328 no_answer=20
- `path_reachable`: raw=0.7347560975609756 kappa=0.44840916998492275 n=328 no_answer=20
- `prerequisite_attribution_valid`: raw=0.9969512195121951 kappa=0.6653061224489777 n=328 no_answer=20
- `respects_artifact_altitude`: raw=0.8323170731707317 kappa=0.4913725047930528 n=328 no_answer=20
- `severity_claim_justified`: raw=0.600609756097561 kappa=0.21998329884181103 n=328 no_answer=20

## Per-attribute agreement (severity)

- `ac_unverifiable`: raw=0.9176829268292683 kappa=0.6576994434137291 n=328 no_answer=20
- `blast_radius`: raw=0.8262195121951219 kappa=0.65012351223894 n=328 no_answer=20
- `debt_impact`: raw=0.6951219512195121 kappa=0.5180722891566265 n=328 no_answer=20
- `divergent_implementation`: raw=0.899390243902439 kappa=0.7604620798017129 n=328 no_answer=20
- `dod_uncertifiable`: raw=0.774390243902439 kappa=0.43506191229866864 n=328 no_answer=20
- `internal_conflict`: raw=0.8689024390243902 kappa=0.658035108136941 n=328 no_answer=20
- `irreversible_without_rationale`: raw=0.9695121951219512 kappa=0.4621187274516236 n=328 no_answer=20
- `likelihood`: raw=0.676829268292683 kappa=0.5084961406881734 n=328 no_answer=20
- `prod_impact`: raw=0.6615853658536586 kappa=0.5012602739726029 n=328 no_answer=20
- `reversibility`: raw=0.8292682926829268 kappa=0.547886873261624 n=328 no_answer=20
- `silent_vs_self_revealing`: raw=0.6371951219512195 kappa=0.41249604888842056 n=328 no_answer=20
- `undecomposed`: raw=0.9878048780487805 kappa=0.7437500000000005 n=328 no_answer=20
- `vague_directive`: raw=0.801829268292683 kappa=0.5752480376140576 n=328 no_answer=20

## Distribution shift

- `validity`: TVD=0.2802 count_delta={'[0.5,0.6)': 27, '[0.0,0.1)': -85, '[0.6,0.7)': 29, '[0.4,0.5)': 16, '[0.9,1.0]': -31, '[0.1,0.2)': 0, '[0.3,0.4)': 5, '[0.8,0.9)': 26, '[0.2,0.3)': 6, '[0.7,0.8)': 7}
- `priority`: TVD=0.1498 count_delta={'[0.5,0.6)': -5, '[0.0,0.1)': -52, '[0.6,0.7)': -1, '[0.4,0.5)': 9, '[0.9,1.0]': -3, '[0.1,0.2)': 11, '[0.3,0.4)': 37, '[0.8,0.9)': -1, '[0.2,0.3)': 1, '[0.7,0.8)': 4}
- `impact` (per underlying categorical question):
  - `ac_unverifiable`: TVD=0.0209 count_delta={'none': 73, 'underspecified_oracle': 18, 'broken_oracle': -2, 'missing_oracle': -3}
  - `blast_radius`: TVD=0.0649 count_delta={'local': 78, 'system': -6, 'module': 14}
  - `debt_impact`: TVD=0.0638 count_delta={'high': -4, 'none': 39, 'low': 48, 'medium': 3}
  - `divergent_implementation`: TVD=0.0538 count_delta={'omits_required_site': -4, 'none': 86, 'contradicts_reality': 2, 'incomplete_enumeration': 2}
  - `dod_uncertifiable`: TVD=0.0788 count_delta={'high': -15, 'uncertifiable_outcome': 10, 'none': 61, 'low': -2, 'medium': -4, 'underspecified_certification': 25, 'certification_cannot_prove': 11}
  - `internal_conflict`: TVD=0.0059 count_delta={'high': 6, 'none': 68, 'low': 9, 'medium': 3}
  - `irreversible_without_rationale`: TVD=0.0056 count_delta={'none': 85, 'low': 2, 'medium': -1}
  - `likelihood`: TVD=0.1282 count_delta={'high': -13, 'low': 84, 'medium': 15}
  - `prod_impact`: TVD=0.1175 count_delta={'high': -8, 'none': 77, 'low': 28, 'medium': -11}
  - `reversibility`: TVD=0.0532 count_delta={'easy': 86, 'hard': 0, 'moderate': 0}
  - `silent_vs_self_revealing`: TVD=0.0470 count_delta={'': 12, 'self_revealing': 58, 'silent': 16}
  - `undecomposed`: TVD=0.0435 count_delta={'missing_required_child': 0, 'none': 68, 'bundles_separable_slices': 18}
  - `vague_directive`: TVD=0.0287 count_delta={'high': -3, 'none': 65, 'low': 19, 'medium': 5}
