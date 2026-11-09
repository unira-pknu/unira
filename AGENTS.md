# User execution preference

- From the user's 2026-11-08 instruction, make necessary code and documentation changes, but provide commands for the user to run application code, ingestion jobs, database operations, deployments, builds, and tests. Do not execute those actions unless the user explicitly asks the agent to execute them.
- Read-only workspace inspection and file editing are allowed. Do not interpret a request to continue implementation as authorization to run a batch or test.
- State which checks were not run and provide the commands needed to perform them.
- The user reiterated this preference on 2026-11-08 after the one-time ZIP execution exception. That exception has ended; defer tests, database comparisons, synchronization, and search checks to user-run commands.
