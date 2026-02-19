"""
Results Writer
==============
Serializes the final AgentState into the required results.json format.
"""
import json
import logging
import os
from typing import Dict, Any

from app.state.agent_state import AgentState
from app.core.output_formatter import format_bug

logger = logging.getLogger(__name__)

class ResultsWriter:
    """
    Service responsible for compiling the full history of the healing run
    into a structured JSON file for the dashboard.
    """

    @staticmethod
    def write_results(state: AgentState, output_path: str = "results.json") -> bool:
        """
        Compile state and write results.json.
        """
        try:
            # 1. Build the structure
            data = {
                "repository": {
                    "url": state.get("repo_url", ""),
                    "team": state.get("team_name", ""),
                    "leader": state.get("leader_name", ""),
                    "branch": state.get("branch_name", ""),
                    "project_type": state.get("project_type", "generic")
                },
                "iterations": [],
                "final_results": {
                    "status": state.get("status", "pending"),
                    "score": state.get("score", 0),
                    "total_bugs_found": state.get("total_bugs_found", 0),
                    "total_fixes_applied": state.get("total_fixes_applied", 0),
                    "summary": state.get("execution_summary", "")
                }
            }

            # 2. Add snapshots
            for snapshot in state.get("snapshots", []):
                # Standard Pydantic snapshots can be converted to dict
                # But IterationSnapshot might have Pydantic objects inside (BugReport, FixResult)
                s_dict = snapshot.model_dump()
                
                # Format bug reports for each iteration using output_formatter
                # This ensures the dashboard shows the "byte-perfect" strings
                formatted_bugs = []
                for bug_dict in s_dict.get("bug_reports", []):
                    # We pass the raw fields to format_bug
                    formatted_bugs.append(format_bug(
                        bug_type=bug_dict["bug_type"],
                        sub_type=bug_dict["sub_type"],
                        file_path=bug_dict["file_path"],
                        line_number=bug_dict["line_number"]
                    ))
                
                s_dict["formatted_bugs"] = formatted_bugs
                data["iterations"].append(s_dict)

            # 3. Write to file
            abs_output = os.path.abspath(output_path)
            logger.info("Writing final results to %s", abs_output)
            
            with open(abs_output, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            
            return True

        except Exception as e:
            logger.error("Failed to write results.json: %s", e, exc_info=True)
            return False
