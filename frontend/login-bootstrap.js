(()=>{
  const form=document.getElementById('loginForm');
  if(!form)return;
  const message=document.getElementById('loginMessage');
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const button=form.querySelector('button[type="submit"]');
    if(button?.disabled)return;
    if(button)button.disabled=true;
    if(message){message.textContent='Validando acesso…';message.classList.remove('success');}
    const fields=new FormData(form);
    const body={company_slug:String(fields.get('company_slug')||'').trim().toLowerCase(),email:String(fields.get('email')||'').trim().toLowerCase(),password:String(fields.get('password')||'')};
    try{
      const response=await fetch('/v1/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});
      let result={};try{result=await response.json();}catch{}
      if(!response.ok)throw new Error(result.detail||`Falha no login (${response.status}).`);
      if(!result.access_token)throw new Error('O servidor não retornou uma sessão válida.');
      try{sessionStorage.setItem('caixa_access_token',result.access_token);}catch{throw new Error('O navegador bloqueou o armazenamento da sessão. Permita o armazenamento para este site e tente novamente.');}
      window.location.replace('/');
    }catch(error){if(message)message.textContent=error.message||'Não foi possível entrar. Verifique os dados e tente novamente.';if(button)button.disabled=false;}
  });
})();
