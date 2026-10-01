import os
from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# ---- workflow configuration (override in .env) ----
# One name per approval level, in order. A document is only "approved" after the LAST level signs off.
# Use a single name (APPROVAL_LEVELS=Reviewer) to get the old one-step behaviour.
LEVELS = [x.strip() for x in os.environ.get("APPROVAL_LEVELS", "Reviewer 1,Reviewer 2,Final Approval").split(",") if x.strip()]
DUE_SOON_DAYS = int(os.environ.get("DUE_SOON_DAYS", "2"))
DEPARTMENTS = [x.strip() for x in os.environ.get("DEPARTMENTS", "General,Finance,HR,Operations,IT,Legal,Marketing").split(",") if x.strip()]


def aware(dt):
    """SQLite hands back naive datetimes; treat them as UTC."""
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    """ISO-8601 string with timezone, or None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")  # "user" | "reviewer"
    approval_level = db.Column(db.Integer, nullable=False, default=1, server_default="1")  # reviewers only: which level they sign off
    department = db.Column(db.String(60))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        d = {"id": self.id, "name": self.name, "email": self.email, "role": self.role, "department": self.department}
        if self.role == "reviewer":
            lvl = min(max(self.approval_level or 1, 1), len(LEVELS))
            d.update(approval_level=lvl, level_name=LEVELS[lvl - 1])
        return d


class Document(db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)
    doc_id = db.Column(db.String(32), unique=True, nullable=False)  # e.g. DOC-20260901-001
    title = db.Column(db.String(120), nullable=False)
    doc_type = db.Column(db.String(40), nullable=False, default="Other")
    description = db.Column(db.Text, nullable=False)
    ai_review = db.Column(db.Boolean, nullable=False, default=True)  # submitter asked for AI pre-review
    file_name = db.Column(db.String(255), nullable=False)      # original name shown to users
    stored_name = db.Column(db.String(80), nullable=False)     # random name on disk
    file_size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    submitted_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    department = db.Column(db.String(60))
    deadline = db.Column(db.DateTime(timezone=True))  # approval deadline (optional)
    current_level = db.Column(db.Integer, nullable=False, default=0, server_default="0")  # 0 = not in review
    current_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    owner = db.relationship("User", foreign_keys=[owner_id])
    versions = db.relationship("DocumentVersion", backref="document", order_by="DocumentVersion.version_no", cascade="all, delete-orphan")
    steps = db.relationship("ApprovalStep", backref="document", order_by="ApprovalStep.id", cascade="all, delete-orphan")
    history = db.relationship(
        "StatusHistory", backref="document", order_by="StatusHistory.id", cascade="all, delete-orphan"
    )
    review = db.relationship("Review", uselist=False, backref="document", cascade="all, delete-orphan")

    def deadline_info(self):
        """pending / due_soon / overdue apply only while the document is waiting for approval."""
        dl, now = aware(self.deadline), utcnow()
        info = {"deadline": iso(dl), "deadline_status": None, "days_left": None}
        if dl:
            info["days_left"] = (dl - now).days if dl > now else -((now - dl).days + 1)
        if self.status == "submitted":
            info["deadline_status"] = "overdue" if dl and dl < now else "due_soon" if dl and dl - now <= timedelta(days=DUE_SOON_DAYS) else "pending"
        return info

    def workflow(self, rbl=None):
        waiting = self.status == "submitted" and 1 <= self.current_level <= len(LEVELS)
        if rbl is None:
            rbl = {}
            for u in User.query.filter_by(role="reviewer"):
                rbl.setdefault(u.approval_level, []).append(u.name)
        names = rbl.get(self.current_level, []) if waiting else []
        mine = {s.level: s for s in self.steps if s.version_no == self.current_version}
        out = []
        for i, name in enumerate(LEVELS, 1):
            s = mine.get(i)
            state = ("rejected" if s.decision == "rejected" else "done" if s.decision == "approved" else "upcoming") if s else (
                "current" if waiting and i == self.current_level else "upcoming")
            out.append({"level": i, "name": name, "state": state, "by": s.reviewer.name if s else None,
                        "at": iso(s.decided_at) if s else None, "remarks": s.remarks if s else ""})
        who = LEVELS[self.current_level - 1] + (": " + ", ".join(names) if names else " (no reviewer assigned yet)") if waiting else None
        return {"total_levels": len(LEVELS), "current_level": self.current_level if waiting else None,
                "current_level_name": LEVELS[self.current_level - 1] if waiting else None,
                "awaiting": who, "awaiting_reviewers": names, "steps": out}

    def to_dict(self, include_history=True, rbl=None):
        ref = self.submitted_at or self.created_at
        if ref.tzinfo is None:  # SQLite returns naive datetimes
            ref = ref.replace(tzinfo=timezone.utc)
        age = (utcnow() - ref).days  # review priority from waiting time
        data = {
            "priority": "High" if age >= 3 else "Medium" if age >= 1 else "Low",
            "id": self.id,
            "doc_id": self.doc_id,
            "title": self.title,
            "doc_type": self.doc_type,
            "description": self.description,
            "ai_review": self.ai_review,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "status": self.status,
            "owner_id": self.owner_id,
            "owner_name": self.owner.name,
            "created_at": iso(self.created_at),
            "submitted_at": iso(self.submitted_at),
            "updated_at": iso(self.updated_at),
            "review": self.review.to_dict() if self.review else None,
            "department": self.department or "General",
            "version": f"V{self.current_version}",
            "version_count": len(self.versions),
            "workflow": self.workflow(rbl),
            **self.deadline_info(),
        }
        if include_history:
            data["versions"] = [v.to_dict() for v in self.versions]
        if include_history:
            data["history"] = [h.to_dict() for h in self.history]
        return data


class StatusHistory(db.Model):
    """One row per status change. Powers the timeline and the History page."""

    __tablename__ = "status_history"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    from_status = db.Column(db.String(20))  # null for the first entry
    to_status = db.Column(db.String(20), nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    changed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    remarks = db.Column(db.Text, nullable=False, default="")

    actor = db.relationship("User", foreign_keys=[changed_by])

    def to_dict(self):
        return {
            "from": self.from_status,
            "to": self.to_status,
            "by": self.changed_by,
            "by_name": self.actor.name,
            "at": iso(self.changed_at),
            "remarks": self.remarks,
        }


class Review(db.Model):
    """The reviewer's final decision. One per document."""

    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), unique=True, nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decision = db.Column(db.String(20), nullable=False)  # "approved" | "rejected"
    remarks = db.Column(db.Text, nullable=False, default="")
    decided_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    reviewer = db.relationship("User", foreign_keys=[reviewer_id])

    def to_dict(self):
        return {
            "reviewer_name": self.reviewer.name,
            "decision": self.decision,
            "remarks": self.remarks,
            "decided_at": iso(self.decided_at),
        }


class DocumentVersion(db.Model):
    """One row per uploaded version (V1, V2, ...). Older versions are never overwritten."""

    __tablename__ = "document_versions"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False)
    file_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(80), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    note = db.Column(db.Text, nullable=False, default="")  # what changed
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    submitted_at = db.Column(db.DateTime(timezone=True))

    author = db.relationship("User", foreign_keys=[created_by])

    def to_dict(self):
        return {"version": f"V{self.version_no}", "version_no": self.version_no, "file_name": self.file_name,
                "file_size": self.file_size, "description": self.description, "note": self.note,
                "by_name": self.author.name, "created_at": iso(self.created_at), "submitted_at": iso(self.submitted_at),
                "is_current": self.version_no == self.document.current_version}


class ApprovalStep(db.Model):
    """A reviewer's decision at one level for one version."""

    __tablename__ = "approval_steps"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    version_no = db.Column(db.Integer, nullable=False)
    level = db.Column(db.Integer, nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decision = db.Column(db.String(20), nullable=False)  # approved | rejected | changes_requested
    remarks = db.Column(db.Text, nullable=False, default="")
    decided_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    reviewer = db.relationship("User", foreign_keys=[reviewer_id])