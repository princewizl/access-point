from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from flask_login import login_required, current_user
from sqlalchemy import func, desc
from functools import wraps

from models import db, User, Complaint
from models_partner import (
    PartnerCompany, PartnerAgent, PartnerProspect,
    ProspectComment, PartnerTicket
)

admin_partner_bp = Blueprint('admin_partner', __name__, url_prefix='/admin/partners')


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            from flask import abort
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


# ─────────────────────────────────────────────
#  LIST — all partner companies
# ─────────────────────────────────────────────

@admin_partner_bp.route('/', methods=['GET'])
@login_required
@admin_required
def list_partners():
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    q        = (request.args.get('q') or '').strip()

    query = PartnerCompany.query.order_by(PartnerCompany.created_at.desc())
    if q:
        query = query.filter(PartnerCompany.name.ilike(f'%{q}%'))

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    companies  = pagination.items

    # Aggregate stats per company in Python (avoids complex subqueries for small datasets)
    stats = {}
    for c in companies:
        agent_count     = PartnerAgent.query.filter_by(company_id=c.id).count()
        prospect_count  = PartnerProspect.query.filter_by(company_id=c.id).count()
        active_count    = PartnerProspect.query.filter_by(company_id=c.id).filter(
            PartnerProspect.stage.in_(['new', 'inprogress'])
        ).count()
        ticket_count    = PartnerTicket.query.filter_by(company_id=c.id).count()
        escalated_count = PartnerTicket.query.filter_by(company_id=c.id, status='escalated').count()
        stats[c.id] = {
            'agents':    agent_count,
            'prospects': prospect_count,
            'active':    active_count,
            'tickets':   ticket_count,
            'escalated': escalated_count,
        }

    # Global KPIs
    total_companies  = PartnerCompany.query.count()
    total_agents     = PartnerAgent.query.count()
    total_prospects  = PartnerProspect.query.count()
    total_escalated  = PartnerTicket.query.filter_by(status='escalated').count()

    return render_template(
        'admin_partners.html',
        companies=companies,
        stats=stats,
        pagination=pagination,
        page=page,
        per_page=per_page,
        total_pages=pagination.pages,
        total=pagination.total,
        q=q,
        kpi=dict(
            companies=total_companies,
            agents=total_agents,
            prospects=total_prospects,
            escalated=total_escalated,
        ),
    )


# ─────────────────────────────────────────────
#  CREATE company
# ─────────────────────────────────────────────

@admin_partner_bp.route('/create', methods=['POST'])
@login_required
@admin_required
def create_partner():
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Company name is required.', 'danger')
        return redirect(url_for('admin_partner.list_partners'))

    if PartnerCompany.query.filter_by(name=name).first():
        flash(f'A partner company named "{name}" already exists.', 'danger')
        return redirect(url_for('admin_partner.list_partners'))

    company = PartnerCompany(name=name)
    db.session.add(company)
    db.session.commit()
    flash(f'Partner company "{name}" created.', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company.id))


# ─────────────────────────────────────────────
#  DETAIL — single company
# ─────────────────────────────────────────────

@admin_partner_bp.route('/<int:company_id>', methods=['GET'])
@login_required
@admin_required
def partner_detail(company_id):
    company = PartnerCompany.query.get_or_404(company_id)

    agents = (
        PartnerAgent.query
        .filter_by(company_id=company_id)
        .join(User, PartnerAgent.user_id == User.id)
        .add_columns(
            User.id.label('user_id'),
            User.first_name, User.last_name,
            User.email, User.username,
            PartnerAgent.id.label('agent_id'),
            PartnerAgent.role,
        )
        .all()
    )

    prospects = (
        PartnerProspect.query
        .filter_by(company_id=company_id)
        .order_by(PartnerProspect.updated_at.desc())
        .all()
    )

    tickets = (
        PartnerTicket.query
        .filter_by(company_id=company_id)
        .order_by(PartnerTicket.created_at.desc())
        .all()
    )

    # Prospect pipeline breakdown
    stage_counts = {s: 0 for s in ['new', 'inprogress', 'completed', 'lost']}
    for p in prospects:
        if p.stage in stage_counts:
            stage_counts[p.stage] += 1

    # Users who are type='partner' and not yet assigned to this company
    assigned_user_ids = {row.user_id for row in agents}
    available_users = (
        User.query
        .filter_by(user_type='partner')
        .filter(~User.id.in_(assigned_user_ids) if assigned_user_ids else True)
        .order_by(User.first_name)
        .all()
    )

    return render_template(
        'admin_partner_detail.html',
        company=company,
        agents=agents,
        prospects=prospects,
        tickets=tickets,
        stage_counts=stage_counts,
        available_users=available_users,
    )


# ─────────────────────────────────────────────
#  EDIT company name
# ─────────────────────────────────────────────

@admin_partner_bp.route('/<int:company_id>/edit', methods=['POST'])
@login_required
@admin_required
def edit_partner(company_id):
    company = PartnerCompany.query.get_or_404(company_id)
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Company name cannot be blank.', 'danger')
        return redirect(url_for('admin_partner.partner_detail', company_id=company_id))

    company.name = name

    new_tier = (request.form.get('tier') or '').strip()
    if new_tier in ('registered', 'certified', 'premier'):
        company.tier = new_tier

    company.nrs_certified = bool(request.form.get('nrs_certified'))

    db.session.commit()
    flash('Company updated.', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  ASSIGN agent to company
# ─────────────────────────────────────────────

@admin_partner_bp.route('/<int:company_id>/agents/assign', methods=['POST'])
@login_required
@admin_required
def assign_agent(company_id):
    company = PartnerCompany.query.get_or_404(company_id)
    user_id = request.form.get('user_id', type=int)
    role    = request.form.get('role', 'agent')

    if not user_id:
        flash('Select a user to assign.', 'danger')
        return redirect(url_for('admin_partner.partner_detail', company_id=company_id))

    user = User.query.get_or_404(user_id)

    # Guard: already assigned somewhere?
    existing = PartnerAgent.query.filter_by(user_id=user_id).first()
    if existing:
        flash(f'{user.first_name} {user.last_name} is already assigned to a partner company.', 'warning')
        return redirect(url_for('admin_partner.partner_detail', company_id=company_id))

    agent = PartnerAgent(user_id=user_id, company_id=company_id, role=role)
    db.session.add(agent)
    db.session.commit()
    flash(f'{user.first_name} {user.last_name} assigned as {role}.', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  REMOVE agent from company
# ─────────────────────────────────────────────

@admin_partner_bp.route('/<int:company_id>/agents/<int:agent_id>/remove', methods=['POST'])
@login_required
@admin_required
def remove_agent(company_id, agent_id):
    agent = PartnerAgent.query.filter_by(id=agent_id, company_id=company_id).first_or_404()
    user  = User.query.get(agent.user_id)
    db.session.delete(agent)
    db.session.commit()
    name = f'{user.first_name} {user.last_name}' if user else 'Agent'
    flash(f'{name} removed from this company.', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  UPDATE prospect stage (admin override)
# ─────────────────────────────────────────────

@admin_partner_bp.route('/prospects/<int:prospect_id>/stage', methods=['POST'])
@login_required
@admin_required
def update_prospect_stage(prospect_id):
    prospect  = PartnerProspect.query.get_or_404(prospect_id)
    new_stage = request.form.get('stage', '').strip()

    if new_stage not in ('new', 'inprogress', 'completed', 'lost'):
        flash('Invalid stage.', 'danger')
        return redirect(url_for('admin_partner.partner_detail', company_id=prospect.company_id))

    prospect.stage = new_stage
    db.session.commit()
    flash(f'Prospect stage updated to "{new_stage}".', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=prospect.company_id))


# ─────────────────────────────────────────────
#  DELETE prospect (admin only)
# ─────────────────────────────────────────────

@admin_partner_bp.route('/prospects/<int:prospect_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_prospect(prospect_id):
    prospect   = PartnerProspect.query.get_or_404(prospect_id)
    company_id = prospect.company_id
    db.session.delete(prospect)
    db.session.commit()
    flash('Prospect removed.', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  UPDATE ticket status
# ─────────────────────────────────────────────

@admin_partner_bp.route('/tickets/<int:ticket_id>/status', methods=['POST'])
@login_required
@admin_required
def update_ticket_status(ticket_id):
    ticket     = PartnerTicket.query.get_or_404(ticket_id)
    new_status = request.form.get('status', '').strip()
    company_id = ticket.company_id

    valid = ('open', 'in_progress', 'escalated', 'resolved', 'closed')
    if new_status not in valid:
        flash('Invalid status.', 'danger')
        return redirect(url_for('admin_partner.partner_detail', company_id=company_id))

    ticket.status = new_status
    db.session.commit()
    flash(f'Ticket status set to "{new_status.replace("_", " ").title()}".', 'success')
    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  ESCALATE ticket → internal Complaint
# ─────────────────────────────────────────────

@admin_partner_bp.route('/tickets/<int:ticket_id>/escalate', methods=['POST'])
@login_required
@admin_required
def escalate_ticket(ticket_id):
    ticket     = PartnerTicket.query.get_or_404(ticket_id)
    company_id = ticket.company_id

    if ticket.status == 'escalated':
        flash('Ticket is already escalated.', 'warning')
        return redirect(url_for('admin_partner.partner_detail', company_id=company_id))

    try:
        internal = Complaint(
            title       = f'[PARTNER-ESC] {ticket.title}',
            category    = 'Partner Escalation',
            description = ticket.description,
            status      = 'open',
            priority    = 'high',
            created_by  = current_user.username,
            user_id     = ticket.opened_by_id,
        )
        db.session.add(internal)
        db.session.flush()

        ticket.status                = 'escalated'
        ticket.internal_complaint_id = internal.id
        db.session.commit()
        flash('Ticket escalated to internal helpdesk.', 'success')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to escalate partner ticket %s', ticket_id)
        flash('Escalation failed. Check server logs.', 'danger')

    return redirect(url_for('admin_partner.partner_detail', company_id=company_id))


# ─────────────────────────────────────────────
#  DELETE company (cascade via model)
# ─────────────────────────────────────────────

@admin_partner_bp.route('/<int:company_id>/delete', methods=['POST'])
@login_required
@admin_required
def delete_partner(company_id):
    company = PartnerCompany.query.get_or_404(company_id)
    name    = company.name
    db.session.delete(company)
    db.session.commit()
    flash(f'Partner company "{name}" and all related records deleted.', 'success')
    return redirect(url_for('admin_partner.list_partners'))
