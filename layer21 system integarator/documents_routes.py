# documents_routes.py (example)
import os
from uuid import uuid4
from flask import (
    Blueprint, current_app, render_template, request, redirect,
    url_for, flash, abort, send_from_directory
)
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import db
from models import Document, Business

bp = Blueprint('documentation', __name__, url_prefix='/documentation')

ALLOWED_EXT = {'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'xlsx', 'csv', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

@bp.route('/', methods=['GET'])
def list_documents():
    """Anyone can view this page.
    - Admins (when authenticated) see all documents.
    - Non-authenticated visitors and normal users see only public docs.
    """

    bus = None

    # Only get business if user is logged in
    if current_user.is_authenticated:
        business_id = current_user.business_id
        bus = Business.query.filter_by(buisness_id=business_id).first()

    q = request.args.get('q', '').strip()

    # Admins see all documents
    if current_user.is_authenticated and getattr(current_user, "is_admin", False):
        query = Document.query
    else:
        # Anonymous or normal users see only public docs
        query = Document.query.filter_by(is_public=True)

    if q:
        query = query.filter(Document.original_filename.ilike(f"%{q}%"))

    docs = query.order_by(Document.created_at.desc()).all()

    return render_template(
        'documents/list.html',
        documents=docs,
        q=q,
        bus=bus
    )

@bp.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_document():
    # If you want upload to be admin-only:
    # if not current_user.is_admin:
    #     abort(403)

    if request.method == 'POST':
        f = request.files.get('file')
        if not f or f.filename == '':
            flash('No file selected', 'danger')
            return redirect(request.url)
        if not allowed_file(f.filename):
            flash('File type not allowed', 'danger')
            return redirect(request.url)

        original = secure_filename(f.filename)
        ext = original.rsplit('.', 1)[1].lower()
        stored = f"{uuid4().hex}.{ext}"
        upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join(current_app.instance_path, 'uploads')
        os.makedirs(upload_folder, exist_ok=True)

        full_path = os.path.join(upload_folder, stored)
        f.save(full_path)

        # Only admins can mark as public from the form; otherwise ignore
        is_public = False
        if current_user.is_admin:
            is_public = bool(request.form.get('is_public'))

        doc = Document(
            original_filename=original,
            stored_filename=stored,
            mimetype=f.mimetype,
            is_public=is_public,
            uploaded_by=current_user
        )
        db.session.add(doc)
        db.session.commit()
        flash('File uploaded', 'success')
        return redirect(url_for('documents.list_documents'))

    # GET
    return render_template('documents/upload.html')

@bp.route('/<int:doc_id>/view', methods=['GET'])
def view_document(doc_id):
    """Serve the file if public OR if the current_user is an admin."""
    doc = Document.query.get_or_404(doc_id)

    # If doc is not public, require admin
    if not doc.is_public:
        # require login + admin
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)

    upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join(current_app.instance_path, 'uploads')
    return send_from_directory(upload_folder, doc.stored_filename, as_attachment=False,
                               mimetype=doc.mimetype,
                               download_name=doc.original_filename)

@bp.route('/<int:doc_id>/download', methods=['GET'])
@login_required
def download_document(doc_id):
    # This route forces download and requires auth (admins or uploader)
    doc = Document.query.get_or_404(doc_id)
    # allow admin or uploader to download even when not public
    if not doc.is_public and not (current_user.is_admin or (doc.uploaded_by_id == current_user.id)):
        abort(403)

    upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join(current_app.instance_path, 'uploads')
    return send_from_directory(upload_folder, doc.stored_filename, as_attachment=True,
                               download_name=doc.original_filename)

@bp.route('/<int:doc_id>/toggle-public', methods=['POST'])
@login_required
def toggle_public(doc_id):
    # admin-only
    if not current_user.is_admin:
        abort(403)
    doc = Document.query.get_or_404(doc_id)
    doc.is_public = not doc.is_public
    db.session.commit()
    flash('Visibility updated', 'success')
    return redirect(url_for('documents.list_documents'))
