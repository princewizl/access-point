from datetime import datetime
from models import db


class PartnerCompany(db.Model):
    """Umbrella Partner Company entity."""
    __tablename__ = 'partner_companies'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(255), nullable=False, unique=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    # Programme tier: 'registered' | 'certified' | 'premier'
    tier          = db.Column(db.String(16), nullable=True)  # NULL treated as 'registered'
    nrs_certified = db.Column(db.Boolean, default=False, nullable=True)

    # Relationships
    agents    = db.relationship("PartnerAgent",    backref="company", cascade="all, delete-orphan")
    prospects = db.relationship("PartnerProspect", backref="company", cascade="all, delete-orphan")
    tickets   = db.relationship("PartnerTicket",   backref="company", cascade="all, delete-orphan")


class PartnerAgent(db.Model):
    """Maps a core User account to a PartnerCompany as an agent."""
    __tablename__ = 'partner_agents'

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    company_id = db.Column(db.Integer, db.ForeignKey('partner_companies.id'), nullable=False)
    role       = db.Column(db.String(32), default='agent')  # 'admin' or 'agent'

    # user.partner_profile → the single PartnerAgent row for this user
    # uselist=False on the backref so current_user.partner_profile is a scalar, not a list
    user = db.relationship(
        "User",
        backref=db.backref("partner_profile", uselist=False)
    )


class PartnerProspect(db.Model):
    __tablename__ = 'partner_prospects'

    id             = db.Column(db.Integer, primary_key=True)
    company_name   = db.Column(db.String(255), nullable=False)
    contact_name   = db.Column(db.String(255), nullable=False)
    email          = db.Column(db.String(150), nullable=False)
    phone          = db.Column(db.String(50),  nullable=True)
    service_type   = db.Column(db.String(64),  nullable=False)
    stage          = db.Column(db.String(32),  default='new', nullable=False)

    company_id         = db.Column(db.Integer, db.ForeignKey('partner_companies.id'), nullable=False)
    assigned_agent_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ✅ Relationship so prospect.assigned_agent works in templates
    assigned_agent = db.relationship("User", foreign_keys=[assigned_agent_id])

    # ✅ Relationship so prospect.comments works as a fallback (route passes comments explicitly)
    comments = db.relationship(
        "ProspectComment",
        backref="prospect",
        cascade="all, delete-orphan",
        order_by="ProspectComment.created_at.asc()"
    )


class ProspectComment(db.Model):
    __tablename__ = 'prospect_comments'

    id          = db.Column(db.Integer, primary_key=True)
    prospect_id = db.Column(db.Integer, db.ForeignKey('partner_prospects.id'), nullable=False)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    message     = db.Column(db.Text, nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    author      = db.relationship("User", foreign_keys=[user_id])
    attachments = db.relationship(
        "ProspectAttachment",
        backref="comment",
        cascade="all, delete-orphan"
    )


class ProspectAttachment(db.Model):
    __tablename__ = 'prospect_attachments'

    id                = db.Column(db.Integer, primary_key=True)
    comment_id        = db.Column(db.Integer, db.ForeignKey('prospect_comments.id'), nullable=False)
    original_filename = db.Column(db.String(260), nullable=False)
    stored_filename   = db.Column(db.String(260), nullable=False)
    file_mime         = db.Column(db.String(120), nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)


class PartnerTicket(db.Model):
    __tablename__ = 'partner_tickets'

    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    # open / in_progress / escalated / resolved / closed
    status      = db.Column(db.String(32), default='open')
    # 'raised'  — partner opened this ticket themselves
    # 'routed'  — auto-created when a client in a partner-referred business raised a complaint
    source      = db.Column(db.String(16), default='raised', nullable=False)

    company_id              = db.Column(db.Integer, db.ForeignKey('partner_companies.id'), nullable=False)
    opened_by_id            = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    internal_complaint_id   = db.Column(db.Integer, db.ForeignKey('complaints.id'), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    opened_by       = db.relationship("User", foreign_keys=[opened_by_id])
    internal_ticket = db.relationship("Complaint", backref="partner_ticket")