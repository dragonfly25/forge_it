---
milestones: []   # milestones whose architecture is included, for example [M1, M2]
components: []   # every component of the system, same form as in docs/architecture/<milestone>.md
interfaces: []   # every interface of the system, same form as in docs/architecture/<milestone>.md
---
# System overview

The system as it is today: the result of every milestone listed above. `/forge-architect <milestone>` updates this file in the same pull request as `docs/architecture/<milestone>.md`.

## Context
What the system does, who uses it, and the external systems it talks to.

## Components
The main components, their responsibilities and their zones.

## Interfaces
The interfaces between components, with producer and consumers.

## Data model
The main entities and where they are stored.

## Decisions
Links to the ADRs in `docs/decisions/` that shape the system.
