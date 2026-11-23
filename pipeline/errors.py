class PipelineError(Exception):
    """A problem the caller can read and act on (bad input, missing file)."""


class BoardError(PipelineError):
    """The board API could not be reached or refused the request."""


class GitHubError(PipelineError):
    """A gh or git command failed."""


class PlanError(PipelineError):
    """The plan agent's answer is not a valid plan."""


class CheckoutError(PipelineError):
    """A local checkout is not in a state a task may be queued on."""
