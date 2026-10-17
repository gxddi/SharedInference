"""
A chat request start to finish: a copy of the model to run on, tokens through it, the job on record.
"""

import secrets

from jinja2 import TemplateError

from audits import Trace, picked
from slices import PACKAGES


class Unavailable(Exception):
    """No whole, ready copy of the model right now."""


class Inference:
    def __init__(self, slices, pipeline, jobs, chat):
        self.slices, self.pipeline, self.jobs, self.chat = slices, pipeline, jobs, chat

    def start(self, user_id, model, messages, settings):
        """Plan a job on a copy of the model: (job id, its output).

        The output yields text as it's generated, then {finish_reason, usage} last;
        it raises pipeline.JobError if a node fails. settings: {max_tokens,
        temperature, top_p, seed}.
        """
        repo = self.slices.repo(model)
        parts = repo and self.slices.replica(model)
        if not parts:
            raise Unavailable(model)
        try:
            prompt = self.chat.prompt(repo, messages)
        except TemplateError as exc:  # e.g. roles in an order the model doesn't take
            raise ValueError(f"The model's chat template refused these messages ({exc}).") from None

        job = secrets.token_hex(16)
        trace = Trace(job, len(parts) - 1, picked())
        self.jobs.start(job, user_id, model, parts, len(prompt), trace.folder is not None)
        return job, self.run(job, model, repo, parts, prompt, settings, trace)

    def run(self, job, model, repo, parts, prompt, settings, trace):
        stop = self.chat.stop(repo)
        output, text, seconds = [], "", {}
        try:
            for token in self.pipeline.run(job, [part["node_id"] for part in parts], prompt, settings, stop, trace,
                                           seconds):
                output.append(token)

                # Only whole characters (a token can end partway through one)
                full = self.chat.text(repo, output)
                if not full.endswith("�") and len(full) > len(text):
                    yield full[len(text):]
                    text = full
        except BaseException:  # a node failed, or the client left
            trace.discard()
            self.jobs.fail(job)
            raise

        # On record (and, if audited, the evidence kept)
        trace.close({"job": job, "model": model, "repo": repo, "packages": PACKAGES,
                     "parts": [{"node": part["node_id"], "layers": [part["start_layer"], part["end_layer"]]}
                               for part in parts],
                     "settings": settings, "prompt": prompt, "output": output, "boundaries": trace.digests()})
        self.jobs.finish(job, model, parts[-1]["end_layer"], prompt, output, trace.digests(), seconds)

        yield {"finish_reason": "stop" if output and output[-1] in stop else "length",
               "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(output),
                         "total_tokens": len(prompt) + len(output)}}
