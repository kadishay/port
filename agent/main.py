import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Bug Triage Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue", type=int, help="Run pipeline for a specific GitHub issue number")
    group.add_argument("--serve", action="store_true", help="Start the webhook server on port 9090")
    args = parser.parse_args()

    if args.issue:
        from agent.orchestrator import run_pipeline
        ctx = run_pipeline(args.issue)
        print(f"\n=== Pipeline complete ===")
        print(f"Severity: {ctx.severity} | Confidence: {ctx.confidence:.0%}")
        print(f"Decision: {ctx.autonomy_decision}")
        print(f"PR: {ctx.pr_url or 'none'}")
    else:
        print("Starting webhook server on port 9090...")
        uvicorn.run("agent.webhook_server:app", host="0.0.0.0", port=9090, reload=False)


if __name__ == "__main__":
    main()
