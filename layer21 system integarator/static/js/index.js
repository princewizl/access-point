(function(){
  const openBtn = document.getElementById('openContactSales');
  const modal = document.getElementById('contactSalesModal');
  const closeBtn = document.getElementById('csCloseBtn');
  const cancelBtn = document.getElementById('csCancelBtn');
  const backdrop = modal ? modal.querySelector('.cs-backdrop') : null;
  const form = document.getElementById('contactSalesForm');
  const submitBtn = document.getElementById('csSubmitBtn');
  const spinner = submitBtn ? submitBtn.querySelector('.cs-spinner') : null;
  const messageEl = document.getElementById('csMessage');

  const firstFocusableSelector = 'input, select, textarea, button, a[href]';
  let lastFocused = null;

  function openModal() {
    lastFocused = document.activeElement;
    modal.classList.remove('hidden');
    // focus first input after short delay
    setTimeout(()=> {
      const first = modal.querySelector('input, select, textarea');
      if(first) first.focus();
      trapFocus(modal);
    }, 60);
    document.body.style.overflow = 'hidden';
  }

  function closeModal() {
    modal.classList.add('hidden');
    document.body.style.overflow = '';
    releaseFocus();
    if(lastFocused) lastFocused.focus();
    // reset form message
    if(messageEl) messageEl.textContent = '';
  }

  function trapFocus(container) {
    const focusable = Array.from(container.querySelectorAll(firstFocusableSelector)).filter(el => !el.hasAttribute('disabled') && el.offsetParent !== null);
    if(!focusable.length) return;
    let idx=0;
    function handler(e) {
      if(e.key === 'Tab') {
        e.preventDefault();
        if(e.shiftKey) idx = (idx - 1 + focusable.length) % focusable.length;
        else idx = (idx + 1) % focusable.length;
        focusable[idx].focus();
      } else if(e.key === 'Escape') {
        closeModal();
      }
    }
    container._focusHandler = handler;
    document.addEventListener('keydown', handler);
  }

  function releaseFocus() {
    if(modal && modal._focusHandler) {
      document.removeEventListener('keydown', modal._focusHandler);
      modal._focusHandler = null;
    }
  }

  // open handlers
  if(openBtn) openBtn.addEventListener('click', openModal);
  if(closeBtn) closeBtn.addEventListener('click', closeModal);
  if(cancelBtn) cancelBtn.addEventListener('click', closeModal);
  if(backdrop) backdrop.addEventListener('click', () => closeModal());

  // form submit
  if(form){
    form.addEventListener('submit', async function(e){
      e.preventDefault();
      messageEl.textContent = '';
      // basic HTML5 validity check
      if(!form.checkValidity()){
        form.reportValidity();
        return;
      }
      // honeypot check
      if(form.querySelector('.hp-field') && form.querySelector('.hp-field').value.trim() !== ''){
        // silently ignore spam
        return;
      }

      // build payload
      const payload = {
        business_name: form.business_name.value.trim(),
        contact_person: form.contact_person.value.trim(),
        email: form.email.value.trim(),
        phone: form.phone.value.trim(),
        alt_phone: form.alt_phone.value.trim(),
        service: form.service.value
      };

      // show spinner
      if(spinner) spinner.classList.remove('hidden');
      submitBtn.setAttribute('disabled', 'disabled');

      try {
        // --- Option A: POST to backend endpoint (uncomment & implement server-side) ---
        /*
        const res = await fetch('/api/contact_sales', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if(!res.ok) throw new Error(data.message || 'Server error');
        */

        // --- Option B: fallback behavior (client-side only) ---
        await new Promise(r=> setTimeout(r, 900)); // simulate network latency

        // log payload for debugging (remove in production)
        console.log('Contact Sales payload:', payload);

        messageEl.style.color = 'green';
        messageEl.textContent = "Thanks — your enquiry has been received. We'll get back to you shortly.";
        form.reset();

        // Optionally close modal after a short delay
        setTimeout(() => { closeModal(); }, 1700);

      } catch(err) {
        console.error(err);
        messageEl.style.color = 'crimson';
        messageEl.textContent = 'Sorry — something went wrong. Please try again or email sales@layer21.example';
      } finally {
        if(spinner) spinner.classList.add('hidden');
        submitBtn.removeAttribute('disabled');
      }
    });
  }

})();
(function(){
    const monthlyBtn = document.getElementById('priceMonthly');
    const yearlyBtn  = document.getElementById('priceYearly');
    const priceEls = document.querySelectorAll('[data-plan]');

    // sample numeric amounts — replace with real amounts if needed
    const priceMap = {
      starter: { month: '₦0', year: '₦0' },    // example
      enterprise: { month: '₦0', year: '₦0' }  // example
    };

    function setMode(mode){
      // mode = 'month' or 'year'
      priceEls.forEach(el => {
        const plan = el.getAttribute('data-plan');
        if(priceMap[plan]){
          el.textContent = priceMap[plan][mode];
        } else {
          // fallback: use existing data attributes if amounts not provided
          el.textContent = el.getAttribute(mode === 'month' ? 'data-month' : 'data-year') || 'On Request';
        }
      });

      if(mode === 'month') {
        monthlyBtn.classList.add('bg-[rgba(15,23,36,0.04)]'); monthlyBtn.classList.remove('bg-[var(--brand-teal)]','text-[var(--brand-dark)]');
        yearlyBtn.classList.remove('bg-[var(--brand-teal)]'); yearlyBtn.classList.add('bg-[rgba(15,23,36,0.04)]');
      } else {
        yearlyBtn.classList.add('bg-[var(--brand-teal)]','text-[var(--brand-dark)]');
        monthlyBtn.classList.remove('bg-[var(--brand-teal)]','text-[var(--brand-dark)]');
      }
    }

    // init as yearly (matches earlier label)
    setMode('year');

    monthlyBtn.addEventListener('click', ()=> setMode('month'));
    yearlyBtn.addEventListener('click', ()=> setMode('year'));
  })();
