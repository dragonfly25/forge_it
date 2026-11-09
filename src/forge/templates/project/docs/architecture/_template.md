---
milestone: M1
status: draft            # draft | ready. Set ready only when the lead agrees and no open question is left.
specs: [specs/m1/feature.md]   # the milestone's specs; at least one
components:              # what the milestone adds or changes; may be []
  - id: example-service  # lowercase letters, digits and -
    zones: [app/example] # zones defined in .forge/zones.yml; may be []
    summary: One line on what the component does
interfaces:              # interfaces the milestone adds or changes; may be []
  - id: IF-example-api   # IF- then lowercase letters, digits and -
    zone: contracts/api  # a zone with contract: true
    producer: example-service
    consumers: []        # component ids from this file or overview.md
    summary: One line on what the interface carries
---
# M1 architecture

## Context
What the milestone delivers, which specs it covers, and which parts of the system it touches.

## Components
For each component: its responsibility, its zones, and what it depends on.

## Interfaces
For each interface: the operations or messages, the data shapes, the errors, and who produces and consumes it.

## Data model
The entities, schemas or stored data the milestone adds or changes. Write "No change" if there is none.

## Decisions
Links to the ADRs in `docs/decisions/` taken for this design, one line each.

## Risks
What could go wrong, and how the plan reduces it.

## Open questions
- (must be empty, or every item marked RESOLVED, for status: ready)
