import os
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import db, Complaint, User, Business
from models_partner import (
    PartnerProspect, ProspectComment, ProspectAttachment,
    PartnerTicket, PartnerAgent, PartnerCompany
)

partner_bp = Blueprint('partner_portal', __name__, url_prefix='/partner')


def get_agent():
    """Return the PartnerAgent row for the current user."""
    return PartnerAgent.query.filter_by(user_id=current_user.id).first()


def partner_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.user_type != 'partner':
            flash("Unauthorized access.", "danger")
            return redirect(url_for('login'))
        if not get_agent():
            flash("Account pending company assignment.", "warning")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function


# ── DASHBOARD ──────────────────────────────────────────────────────────────────

@partner_bp.route('/dashboard')
@login_required
@partner_required
def dashboard():
    company_id = get_agent().company_id

    active_prospects_count = PartnerProspect.query.filter_by(company_id=company_id) \
        .filter(PartnerProspect.stage.in_(['new', 'inprogress'])).count()

    completed_prospects_count = PartnerProspect.query.filter_by(
        company_id=company_id, stage='completed'
    ).count()

    escalated_tickets_count = PartnerTicket.query.filter_by(
        company_id=company_id, status='escalated'
    ).count()

    recent_prospects = PartnerProspect.query.filter_by(company_id=company_id) \
        .order_by(PartnerProspect.updated_at.desc()).limit(5).all()

    return render_template(
        'partner/partner_dashboard.html',
        active_prospects_count=active_prospects_count,
        completed_prospects_count=completed_prospects_count,
        escalated_tickets_count=escalated_tickets_count,
        recent_prospects=recent_prospects
    )


# ── PROSPECTS ──────────────────────────────────────────────────────────────────

@partner_bp.route('/prospects')
@login_required
@partner_required
def list_prospects():
    view_type = request.args.get('view', 'kanban')
    company_id = get_agent().company_id

    prospects = PartnerProspect.query.filter_by(company_id=company_id) \
        .order_by(PartnerProspect.created_at.desc()).all()

    kanban_data = {'new': [], 'inprogress': [], 'completed': [], 'lost': []}
    for p in prospects:
        if p.stage in kanban_data:
            kanban_data[p.stage].append(p)

    return render_template(
        'partner/prospects.html',
        prospects=prospects,
        kanban_data=kanban_data,
        view_type=view_type
    )


@partner_bp.route('/prospects/add', methods=['POST'])
@login_required
@partner_required
def add_prospect():
    agent = get_agent()
    company_id = agent.company_id

    new_prospect = PartnerProspect(
        company_name=request.form.get('company_name', '').strip(),
        contact_name=request.form.get('contact_name', '').strip(),
        email=request.form.get('email', '').strip().lower(),
        phone=request.form.get('phone', '').strip(),
        service_type=request.form.get('service_type'),
        stage='new',
        company_id=company_id,
        assigned_agent_id=current_user.id
    )
    db.session.add(new_prospect)
    db.session.commit()
    flash("Prospect added to the pipeline.", "success")
    return redirect(url_for('partner_portal.list_prospects'))


@partner_bp.route('/prospects/<int:prospect_id>', methods=['GET', 'POST'])
@login_required
@partner_required
def prospect_detail(prospect_id):
    company_id = get_agent().company_id
    prospect = PartnerProspect.query.filter_by(
        id=prospect_id, company_id=company_id
    ).first_or_404()

    if request.method == 'POST':
        message_text = request.form.get('message', '').strip()
        if not message_text:
            flash("Update text cannot be blank.", "warning")
            return redirect(url_for('partner_portal.prospect_detail', prospect_id=prospect.id))

        comment = ProspectComment(
            prospect_id=prospect.id,
            user_id=current_user.id,
            message=message_text
        )
        db.session.add(comment)
        db.session.flush()

        if 'attachments' in request.files:
            for file in request.files.getlist('attachments'):
                if file and file.filename:
                    orig = secure_filename(file.filename)
                    stored = f"prospect_{prospect.id}_{secrets.token_hex(8)}_{orig}"
                    upload_folder = current_app.config.get(
                        'UPLOAD_FOLDER',
                        os.path.join(current_app.root_path, 'uploads')
                    )
                    os.makedirs(upload_folder, exist_ok=True)
                    try:
                        file.save(os.path.join(upload_folder, stored))
                        db.session.add(ProspectAttachment(
                            comment_id=comment.id,
                            original_filename=orig,
                            stored_filename=stored,
                            file_mime=file.mimetype
                        ))
                    except Exception as e:
                        current_app.logger.exception(f"Upload failed: {e}")
                        flash(f"Could not upload {orig}.", "danger")

        db.session.commit()
        flash("Update posted to the timeline.", "success")
        return redirect(url_for('partner_portal.prospect_detail', prospect_id=prospect.id))

    # All agents in this company for the assign-agent dropdown
    company_agents = PartnerAgent.query.filter_by(company_id=company_id).all()

    # Comments ordered oldest-first for a natural timeline reading
    comments = ProspectComment.query \
        .filter_by(prospect_id=prospect.id) \
        .order_by(ProspectComment.created_at.asc()) \
        .all()

    return render_template(
        'partner/prospect_detail.html',
        prospect=prospect,
        comments=comments,
        company_agents=company_agents
    )


@partner_bp.route('/prospects/<int:prospect_id>/update-stage', methods=['POST'])
@login_required
@partner_required
def update_stage(prospect_id):
    company_id = get_agent().company_id
    prospect = PartnerProspect.query.filter_by(
        id=prospect_id, company_id=company_id
    ).first_or_404()

    new_stage = request.form.get('stage')
    if new_stage in ['new', 'inprogress', 'completed', 'lost']:
        prospect.stage = new_stage
        db.session.commit()
        flash("Stage updated.", "success")
    else:
        flash("Invalid stage.", "danger")

    return redirect(url_for('partner_portal.prospect_detail', prospect_id=prospect_id))


@partner_bp.route('/prospects/<int:prospect_id>/assign-agent', methods=['POST'])
@login_required
@partner_required
def assign_agent(prospect_id):
    company_id = get_agent().company_id
    prospect = PartnerProspect.query.filter_by(
        id=prospect_id, company_id=company_id
    ).first_or_404()

    agent_user_id = request.form.get('agent_user_id', type=int)
    if agent_user_id:
        # Verify this user belongs to the same company
        target_agent = PartnerAgent.query.filter_by(
            user_id=agent_user_id, company_id=company_id
        ).first()
        if target_agent:
            prospect.assigned_agent_id = agent_user_id
            db.session.commit()
            flash("Agent assigned successfully.", "success")
        else:
            flash("Selected agent is not part of your company.", "danger")
    else:
        prospect.assigned_agent_id = None
        db.session.commit()
        flash("Agent unassigned.", "info")

    return redirect(url_for('partner_portal.prospect_detail', prospect_id=prospect_id))


# ── TICKETS ────────────────────────────────────────────────────────────────────

@partner_bp.route('/tickets')
@login_required
@partner_required
def ticket_dashboard():
    company_id = get_agent().company_id
    all_tickets = (PartnerTicket.query
                   .filter_by(company_id=company_id)
                   .order_by(PartnerTicket.created_at.desc())
                   .all())
    # Separate partner-raised tickets from client tickets auto-routed to this partner
    raised_tickets = [t for t in all_tickets if t.source == 'raised']
    routed_tickets = [t for t in all_tickets if t.source == 'routed']
    return render_template('partner/tickets.html',
                           tickets=all_tickets,
                           raised_tickets=raised_tickets,
                           routed_tickets=routed_tickets)


@partner_bp.route('/tickets/raise', methods=['POST'])
@login_required
@partner_required
def raise_ticket():
    agent = get_agent()
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()

    if not title or not description:
        flash("Title and description are required.", "warning")
        return redirect(url_for('partner_portal.ticket_dashboard'))

    ticket = PartnerTicket(
        title=title,
        description=description,
        status='open',
        company_id=agent.company_id,
        opened_by_id=current_user.id
    )
    db.session.add(ticket)
    db.session.commit()
    flash("Support ticket submitted.", "success")
    return redirect(url_for('partner_portal.ticket_dashboard'))


@partner_bp.route('/tickets/<int:ticket_id>')
@login_required
@partner_required
def ticket_detail(ticket_id):
    company_id = get_agent().company_id
    ticket = PartnerTicket.query.filter_by(
        id=ticket_id, company_id=company_id
    ).first_or_404()
    return render_template('partner/ticket_detail.html', ticket=ticket)


@partner_bp.route('/tickets/<int:ticket_id>/escalate', methods=['POST'])
@login_required
@partner_required
def escalate_ticket(ticket_id):
    agent      = get_agent()
    company_id = agent.company_id
    ticket     = PartnerTicket.query.filter_by(id=ticket_id, company_id=company_id).first_or_404()
    partner    = PartnerCompany.query.get(company_id)

    if ticket.status == 'escalated':
        flash("Ticket is already escalated.", "warning")
        next_url = request.form.get('next') or url_for('partner_portal.ticket_dashboard')
        return redirect(next_url)

    try:
        if ticket.source == 'routed' and ticket.internal_complaint_id:
            # Ticket was auto-routed from a client complaint — escalate the existing complaint
            internal = Complaint.query.get(ticket.internal_complaint_id)
            if internal:
                internal.status   = 'escalated'
                internal.priority = 'high'
        else:
            # Partner-raised ticket — create a new internal complaint
            internal = Complaint(
                title=f"[PARTNER-ESC] {ticket.title}",
                category="Partner Escalation Tier-1",
                description=ticket.description,
                status='escalated',
                priority='high',
                created_by=current_user.username,
                user_id=current_user.id,
            )
            db.session.add(internal)
            db.session.flush()
            ticket.internal_complaint_id = internal.id

        ticket.status = 'escalated'
        db.session.commit()

        # Notify Layer21 support
        from app import _notify_l21_escalation
        if internal:
            _notify_l21_escalation(internal, partner.name if partner else 'Partner')

        flash("Ticket escalated to Layer21 support.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Escalation failed.")
        flash("Escalation failed due to a system error.", "danger")

    next_url = request.form.get('next') or url_for('partner_portal.ticket_dashboard')
    return redirect(next_url)


@partner_bp.route('/tickets/<int:ticket_id>/status', methods=['POST'])
@login_required
@partner_required
def update_partner_ticket_status(ticket_id):
    company_id = get_agent().company_id
    ticket = PartnerTicket.query.filter_by(id=ticket_id, company_id=company_id).first_or_404()
    new_status = request.form.get('status', '').strip()
    allowed = ('open', 'in_progress', 'resolved', 'closed')
    if new_status in allowed and ticket.status not in ('escalated', 'closed'):
        ticket.status = new_status
        # Mirror status to the linked internal complaint so L21 dashboard stays in sync
        if ticket.internal_complaint_id:
            internal = Complaint.query.get(ticket.internal_complaint_id)
            if internal and internal.status not in ('escalated', 'closed'):
                internal.status = new_status
        db.session.commit()
        flash(f'Ticket status updated to "{new_status.replace("_", " ").title()}".', 'success')
    else:
        flash('Invalid status or ticket cannot be updated at this stage.', 'warning')
    next_url = request.form.get('next') or url_for('partner_portal.ticket_detail', ticket_id=ticket_id)
    return redirect(next_url)


@partner_bp.route('/tickets/<int:ticket_id>/close', methods=['POST'])
@login_required
@partner_required
def close_ticket(ticket_id):
    company_id = get_agent().company_id
    ticket = PartnerTicket.query.filter_by(
        id=ticket_id, company_id=company_id
    ).first_or_404()

    if ticket.status in ['resolved', 'closed']:
        flash("Ticket is already closed.", "warning")
    else:
        ticket.status = 'closed'
        db.session.commit()
        flash("Ticket closed.", "success")

    next_url = request.form.get('next') or url_for('partner_portal.ticket_dashboard')
    return redirect(next_url)