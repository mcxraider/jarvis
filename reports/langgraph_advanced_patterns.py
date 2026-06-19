"""
Advanced LangGraph Patterns
============================
Every Python code block extracted from 05-advanced-patterns.md, in the order
they appear in the document, with section headers and comments added for
readability. No code has been removed or altered from the source — including
two incomplete "exercise" stubs (left as `pass`) and a starter template.

Known issues inherited from the source document (flagged, not fixed, since
nothing should be left out or silently changed):
  1. `create_advanced_service_graph()` registers a node called
     "recover_from_checkpoint" mapped to a function `recover_workflow_state`
     that is never defined anywhere in the source material.
  2. That same "recover_from_checkpoint" node is never wired up with an edge,
     so it's unreachable even if the function existed.
"""

import os
import time
import random
import pickle
from pathlib import Path
from datetime import datetime
from functools import wraps
from typing import TypedDict, Annotated, List, Dict, Optional

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import StateGraph, END


# ======================================================================
# Step 1: Define the Advanced State
# ======================================================================
class AdvancedServiceState(TypedDict):
    """State schema for the robust customer service workflow."""
    messages: Annotated[List[BaseMessage], "Conversation messages"]
    customer_id: Annotated[str, "Customer ID"]
    request_type: Annotated[str, "Type of request"]
    processing_stage: Annotated[str, "Current processing stage"]
    error_count: Annotated[int, "Number of errors encountered"]
    retry_count: Annotated[int, "Number of retries attempted"]
    human_intervention: Annotated[bool, "Whether human intervention is needed"]
    checkpoint_data: Annotated[Dict, "Checkpoint data for recovery"]
    next: Annotated[str, "Next action"]


# ======================================================================
# Step 2: Implement Circuit Breaker
# ======================================================================
class CircuitBreaker:
    """Prevents cascading failures by monitoring system health.

    Three states:
      - CLOSED:    normal operation, calls pass through
      - OPEN:      too many failures, calls are rejected immediately
      - HALF_OPEN: recovery timeout elapsed, next call is a trial run
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def call(self, func, *args, **kwargs):
        """Execute function with circuit breaker protection."""
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
            else:
                raise Exception("Circuit breaker is OPEN")

        try:
            result = func(*args, **kwargs)
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.failure_count = 0
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()

            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"

            raise e


# Usage
circuit_breaker = CircuitBreaker()


# ======================================================================
# Step 3: Implement Retry Pattern
# ======================================================================
def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """Retry decorator with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    if attempt == max_retries:
                        raise last_exception

                    # Exponential backoff with jitter
                    delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                    time.sleep(delay)

            raise last_exception
        return wrapper
    return decorator


@retry_with_backoff(max_retries=3)
def unreliable_operation():
    """Simulate an unreliable operation."""
    if random.random() < 0.7:  # 70% failure rate
        raise Exception("Operation failed")
    return "Operation succeeded"


# ======================================================================
# Step 4: Implement Checkpoint System
# ======================================================================
class CheckpointManager:
    """Saves and restores workflow state to/from disk via pickle files."""

    def __init__(self, checkpoint_dir: str = "checkpoints"):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(exist_ok=True)

    def save_checkpoint(self, state: AdvancedServiceState, checkpoint_id: str):
        """Save state to a checkpoint file."""
        checkpoint_data = {
            "state": state,
            "timestamp": datetime.now().isoformat(),
            "checkpoint_id": checkpoint_id
        }

        filepath = self.checkpoint_dir / f"{checkpoint_id}.pkl"
        with open(filepath, 'wb') as f:
            pickle.dump(checkpoint_data, f)

    def load_checkpoint(self, checkpoint_id: str) -> Optional[AdvancedServiceState]:
        """Load state from a checkpoint file."""
        filepath = self.checkpoint_dir / f"{checkpoint_id}.pkl"

        if filepath.exists():
            with open(filepath, 'rb') as f:
                checkpoint_data = pickle.load(f)
                return checkpoint_data["state"]

        return None

    def delete_checkpoint(self, checkpoint_id: str):
        """Delete a checkpoint file."""
        filepath = self.checkpoint_dir / f"{checkpoint_id}.pkl"
        if filepath.exists():
            filepath.unlink()


# Usage
checkpoint_manager = CheckpointManager()


# ======================================================================
# Step 5: Implement Human-in-the-Loop
# ======================================================================
class HumanInterventionManager:
    """Queues requests for human approval and tracks their resolution."""

    def __init__(self):
        self.intervention_queue = []
        self.approved_requests = {}

    def request_human_approval(self, request_id: str, request_data: dict) -> str:
        """Request human approval for a specific request."""
        intervention_request = {
            "request_id": request_id,
            "request_data": request_data,
            "timestamp": datetime.now().isoformat(),
            "status": "pending"
        }

        self.intervention_queue.append(intervention_request)

        # In a real system, this would notify human agents
        print(f"Human approval requested for request {request_id}")

        return "waiting_for_approval"

    def approve_request(self, request_id: str, approved: bool, notes: str = ""):
        """Approve or reject a request."""
        self.approved_requests[request_id] = {
            "approved": approved,
            "notes": notes,
            "timestamp": datetime.now().isoformat()
        }

    def check_approval_status(self, request_id: str) -> Optional[dict]:
        """Check the approval status of a request."""
        return self.approved_requests.get(request_id)


# Usage
human_manager = HumanInterventionManager()


# ======================================================================
# Step 6: Create the Advanced Graph
# ======================================================================
def create_advanced_service_graph():
    """Create a robust customer service workflow with advanced patterns."""
    workflow = StateGraph(AdvancedServiceState)

    # Add nodes with advanced patterns
    workflow.add_node("process_request", process_with_circuit_breaker)
    workflow.add_node("handle_error", handle_error_with_retry)
    workflow.add_node("request_human_approval", request_human_intervention)
    workflow.add_node("check_approval", check_human_approval)
    workflow.add_node("save_checkpoint", save_workflow_checkpoint)
    # NOTE (inherited from source): `recover_workflow_state` is referenced
    # here but never defined in the original document.
    workflow.add_node("recover_from_checkpoint", recover_workflow_state)

    # Set entry point
    workflow.set_entry_point("process_request")

    # Add conditional edges for error handling and human intervention
    workflow.add_conditional_edges(
        "process_request",
        lambda state: state.get("next", "complete"),
        {
            "error": "handle_error",
            "human_approval_needed": "request_human_approval",
            "checkpoint": "save_checkpoint",
            "complete": END
        }
    )

    workflow.add_conditional_edges(
        "handle_error",
        lambda state: state.get("next", "complete"),
        {
            "retry": "process_request",
            "human_intervention": "request_human_approval",
            "fail": END
        }
    )

    workflow.add_edge("request_human_approval", "check_approval")
    workflow.add_edge("check_approval", "process_request")
    workflow.add_edge("save_checkpoint", "process_request")
    # NOTE (inherited from source): "recover_from_checkpoint" has no edges,
    # so it is unreachable in this graph as written.

    return workflow.compile()


def process_with_circuit_breaker(state: AdvancedServiceState) -> AdvancedServiceState:
    """Process request with circuit breaker protection."""
    try:
        # Simulate processing with potential failure
        result = circuit_breaker.call(unreliable_operation)

        return {
            **state,
            "processing_stage": "completed",
            "next": "complete"
        }
    except Exception as e:
        return {
            **state,
            "error_count": state["error_count"] + 1,
            "next": "error"
        }


def handle_error_with_retry(state: AdvancedServiceState) -> AdvancedServiceState:
    """Handle errors with retry logic."""
    retry_count = state["retry_count"]

    if retry_count < 3:
        return {
            **state,
            "retry_count": retry_count + 1,
            "next": "retry"
        }
    else:
        return {
            **state,
            "human_intervention": True,
            "next": "human_intervention"
        }


def request_human_intervention(state: AdvancedServiceState) -> AdvancedServiceState:
    """Request human intervention for complex cases."""
    request_id = f"req_{int(time.time())}"

    human_manager.request_human_approval(
        request_id,
        {"customer_id": state["customer_id"], "request_type": state["request_type"]}
    )

    return {
        **state,
        "next": "waiting_for_approval"
    }


def check_human_approval(state: AdvancedServiceState) -> AdvancedServiceState:
    """Check if human approval has been received."""
    # In a real system, this would check the approval status
    # For demo purposes, we'll simulate approval
    return {
        **state,
        "next": "process_request"
    }


def save_workflow_checkpoint(state: AdvancedServiceState) -> AdvancedServiceState:
    """Save workflow state to checkpoint."""
    checkpoint_id = f"checkpoint_{state['customer_id']}_{int(time.time())}"
    checkpoint_manager.save_checkpoint(state, checkpoint_id)

    return {
        **state,
        "checkpoint_data": {"checkpoint_id": checkpoint_id},
        "next": "process_request"
    }


# Usage
app = create_advanced_service_graph()

# Initialize state
initial_state = {
    "messages": [HumanMessage(content="I need help with a complex technical issue")],
    "customer_id": "CUST123",
    "request_type": "technical_support",
    "processing_stage": "initial",
    "error_count": 0,
    "retry_count": 0,
    "human_intervention": False,
    "checkpoint_data": {},
    "next": ""
}

# Run the advanced system
result = app.invoke(initial_state)


# ======================================================================
# Exercise 1: Circuit Breaker Implementation — starter code (incomplete)
# ======================================================================
class AdvancedCircuitBreaker:
    """Starter code from the tutorial's Exercise 1. The `call` body is
    intentionally left as `pass` in the source — it's a challenge for the
    reader to extend CircuitBreaker with per-failure-type tracking."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"
        self.failure_types = {}  # Track different types of failures

    def call(self, func, *args, **kwargs):
        """Execute function with advanced circuit breaker protection."""
        # Your advanced circuit breaker logic here
        pass


# ======================================================================
# Exercise 2: Distributed Checkpointing — solution
# ======================================================================
class DistributedCheckpointManager:
    """Saves/loads checkpoints to a distributed backend (e.g. Redis).
    Storage calls are stubbed with prints in this tutorial version —
    a real implementation would talk to Redis, MongoDB, etc."""

    def __init__(self, storage_backend: str = "redis"):
        self.storage_backend = storage_backend
        self.node_id = f"node_{os.getpid()}"

    def save_distributed_checkpoint(self, state: dict, checkpoint_id: str):
        """Save checkpoint to distributed storage."""
        checkpoint_data = {
            "state": state,
            "node_id": self.node_id,
            "timestamp": datetime.now().isoformat(),
            "checkpoint_id": checkpoint_id
        }

        # In a real implementation, this would use Redis, MongoDB, etc.
        print(f"Saving checkpoint {checkpoint_id} to {self.storage_backend}")

    def load_distributed_checkpoint(self, checkpoint_id: str) -> Optional[dict]:
        """Load checkpoint from distributed storage."""
        # In a real implementation, this would retrieve from distributed storage
        print(f"Loading checkpoint {checkpoint_id} from {self.storage_backend}")
        return None


# ======================================================================
# Exercise 3: Advanced Human-in-the-Loop — role-based approvals (solution)
# ======================================================================
class RoleBasedApprovalSystem:
    """Routes approval requests through a sequence of roles depending on
    request type (e.g. security issues need 3 sequential sign-offs,
    billing needs 2, everything else defaults to a single general agent)."""

    def __init__(self):
        self.approval_workflows = {
            "technical_issue": ["tech_support", "senior_tech"],
            "billing_issue": ["billing_agent", "billing_manager"],
            "security_issue": ["security_analyst", "security_manager", "compliance_officer"]
        }
        self.approval_status = {}

    def request_approval(self, request_type: str, request_data: dict) -> str:
        """Request approval based on request type and workflow."""
        workflow = self.approval_workflows.get(request_type, ["general_agent"])
        request_id = f"req_{int(time.time())}"

        self.approval_status[request_id] = {
            "workflow": workflow,
            "current_step": 0,
            "approvals": [],
            "request_data": request_data,
            "status": "pending"
        }

        return request_id

    def approve_step(self, request_id: str, approver_role: str, approved: bool):
        """Approve or reject the current step in the workflow.

        Advances `current_step` on approval (unless it's the final step,
        in which case the whole request becomes "approved"); any rejection
        immediately marks the request "rejected".
        """
        if request_id not in self.approval_status:
            raise ValueError(f"Request {request_id} not found")

        status = self.approval_status[request_id]
        workflow = status["workflow"]
        current_step = status["current_step"]

        if approver_role != workflow[current_step]:
            raise ValueError(f"Invalid approver role: {approver_role}")

        status["approvals"].append({
            "role": approver_role,
            "approved": approved,
            "timestamp": datetime.now().isoformat()
        })

        if approved and current_step < len(workflow) - 1:
            status["current_step"] += 1
        elif not approved:
            status["status"] = "rejected"
        else:
            status["status"] = "approved"


# ======================================================================
# Debugging Tips
# ======================================================================
def debug_advanced_patterns(state: AdvancedServiceState, pattern_name: str):
    """Debug advanced pattern execution."""
    print(f"Pattern: {pattern_name}")
    print(f"Error count: {state.get('error_count', 0)}")
    print(f"Retry count: {state.get('retry_count', 0)}")
    print(f"Human intervention: {state.get('human_intervention', False)}")
    print(f"Processing stage: {state.get('processing_stage', 'unknown')}")
    print("-" * 50)


def safe_advanced_execution(func, state: AdvancedServiceState, pattern_name: str):
    """Execute advanced pattern with debugging and error handling."""
    try:
        debug_advanced_patterns(state, pattern_name)
        result = func(state)
        return result
    except Exception as e:
        print(f"Advanced pattern {pattern_name} failed: {e}")
        return state  # Return original state on error


# ======================================================================
# Try It Yourself: E-commerce Order Processing — starter template (incomplete)
# ======================================================================
class OrderProcessingState(TypedDict):
    """State schema for the order-processing challenge."""
    order_id: Annotated[str, "Order ID"]
    customer_id: Annotated[str, "Customer ID"]
    order_amount: Annotated[float, "Order amount"]
    payment_status: Annotated[str, "Payment status"]
    processing_stage: Annotated[str, "Processing stage"]
    error_count: Annotated[int, "Error count"]
    human_approval_needed: Annotated[bool, "Human approval needed"]
    next: Annotated[str, "Next action"]


class PaymentProcessor:
    """Starter stub from the challenge — body intentionally left unimplemented."""

    def process_payment(self, state: OrderProcessingState) -> OrderProcessingState:
        """Process payment with circuit breaker protection."""
        # Your payment processing logic here
        pass


class OrderApprovalManager:
    """Starter stub from the challenge — body intentionally left unimplemented."""

    def check_approval_required(self, state: OrderProcessingState) -> bool:
        """Check if order requires human approval."""
        # Your approval logic here
        pass