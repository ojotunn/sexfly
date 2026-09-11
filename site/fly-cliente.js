// Cliente do FLY, compartilhado pela pagina publica e pela /dev:
//  1. decodeDV: indices dos neuronios que dispararam, em delta-varint (ordenados, diferenca em varint de 7 bits)
//  2. Corpo3D: a mosca desenhada no navegador em three.js a partir do modelo exportado (fly-model.json/bin) e dos
//     quadros de pose (qpos inteiro + camera) que corpo/corpo.py manda 30x por segundo; cinematica direta igual
//     ao mj_kinematics do MuJoCo (pais antes dos filhos, juntas na ordem, dobradica em torno do proprio ponto).
window.decodeDV=function(u8){
  const out=new Uint32Array(u8.length); let n=0, acc=0, i=0;
  while(i<u8.length){ let v=0, s=0, b; do{ b=u8[i++]; v|=(b&0x7F)<<s; s+=7; }while((b&0x80) && i<u8.length); acc+=v; out[n++]=acc; }
  return out.subarray(0,n);
};

window.Corpo3D=(function(){
  const ATRASO_MS=50;                 // renderiza um pouco atras do ultimo quadro para interpolar entre dois
  const S={pronto:false, ren:null, scene:null, cam:null, mosca:null, espelho:null, corpos:[], objs:[], objsE:[],
           juntas:[], matriz:[], q:[null,null], t:[0,0], cams:[null,null], nq:0, fovy:45, canvas:null, box:null,
           ultimo:0, qi:null, erro:null, macho:null, espelhoM:null, objsM:[], objsME:[], matrizM:[], raiz:-1, qM:null,
           sexo:{libido:0, estado:'idle', ritmo:1, t0:performance.now(), t_est:performance.now(), pose:null, alvo:null, dnEle:{}}, asas:{}};
  const M=new THREE.Matrix4(), M2=new THREE.Matrix4(), M3=new THREE.Matrix4(), Q=new THREE.Quaternion();
  const V=new THREE.Vector3(), V2=new THREE.Vector3(), UM=new THREE.Vector3(1,1,1);

  function TR(pos, quat, out){ return out.compose(V.set(pos[0],pos[1],pos[2]), Q.set(quat[1],quat[2],quat[3],quat[0]), UM); }

  async function init(canvas){
    S.canvas=canvas; S.box=canvas.parentElement;
    try{
      const [j,bin]=await Promise.all([fetch('/static/fly-model.json').then(r=>r.json()), fetch('/static/fly-model.bin').then(r=>r.arrayBuffer())]);
      montar(j,bin); S.pronto=true; requestAnimationFrame(loop);
    }catch(e){ S.erro=e; console.error('Corpo3D', e); }
  }

  function montar(j,bin){
    S.nq=j.nq; S.fovy=(j.cam&&j.cam.fovy)||45;
    S.ren=new THREE.WebGLRenderer({canvas:S.canvas, antialias:true, alpha:false, powerPreference:'high-performance'});
    S.ren.setClearColor(0x000000,1); S.ren.outputColorSpace=THREE.SRGBColorSpace;
    S.scene=new THREE.Scene(); S.scene.background=new THREE.Color(0x000000);
    S.cam=new THREE.PerspectiveCamera(S.fovy, 2, 0.3, 500); S.cam.up.set(0,0,1);
    // luzes: o palco do MuJoCo tem headlight + um sol de cima um pouco de lado
    S.scene.add(new THREE.HemisphereLight(0xffffff, 0x14141a, 0.85));
    const sol=new THREE.DirectionalLight(0xfff4e2, 1.5); sol.position.set(-3,-4,10); S.scene.add(sol);
    const contra=new THREE.DirectionalLight(0x9fb8ff, 0.35); contra.position.set(6,3,3); S.scene.add(contra);
    const loader=new THREE.TextureLoader(); const texs={}, mats={}, matsE={};
    for(const [k,m] of Object.entries(j.materiais)){
      let map=null;
      // cor de reserva castanha: se a textura nao carregar, a mosca fica lisa em vez de preta (sem textura o
      // three.js amostra um mapa vazio = preto sobre fundo preto, e ela some)
      const reserva=m.tex?new THREE.Color(0.52,0.40,0.28):new THREE.Color(m.rgba[0],m.rgba[1],m.rgba[2]);
      const cor=new THREE.Color(m.rgba[0],m.rgba[1],m.rgba[2]); const a=m.rgba[3];
      mats[k]=new THREE.MeshStandardMaterial({color:m.tex?reserva:cor, transparent:a<0.999, opacity:a, roughness:Math.max(0.3,1-0.7*(m.shininess||0.3)), metalness:0.0, side:THREE.DoubleSide});
      // reflexo no chao: copia escura e translucida, espelhada em z
      matsE[k]=new THREE.MeshBasicMaterial({color:(m.tex?reserva:cor).clone().multiplyScalar(0.5), transparent:true, opacity:0.20*a, side:THREE.DoubleSide, depthWrite:false});
      if(m.tex){
        const aplicar=(t)=>{ for(const mm of [mats[k],matsE[k]]){ mm.map=t; mm.color.copy(mm===mats[k]?cor:cor.clone().multiplyScalar(0.5)); mm.needsUpdate=true; } };
        if(texs[m.tex]){ const t=texs[m.tex]; if(t.image) aplicar(t); else t.__esperando.push(aplicar); }
        else{
          const t=loader.load('/static/fly-tex/'+m.tex+'.png', (tx)=>{ (tx.__esperando||[]).forEach(f=>f(tx)); tx.__esperando=[]; }, undefined, ()=>console.warn('textura nao carregou:', m.tex));
          t.flipY=false; t.colorSpace=THREE.SRGBColorSpace; t.anisotropy=4; t.__esperando=[aplicar]; texs[m.tex]=t;
        }
      }
    }
    const geos=j.malhas.map(ml=>{
      const g=new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(bin, ml.off_pos, ml.nv*3),3));
      if(ml.uv) g.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(bin, ml.off_uv, ml.nv*2),2));
      g.setIndex(new THREE.BufferAttribute(ml.idx16?new Uint16Array(bin, ml.off_idx, ml.nf*3):new Uint32Array(bin, ml.off_idx, ml.nf*3),1));
      g.computeVertexNormals(); return g;
    });
    S.mosca=new THREE.Group(); S.espelho=new THREE.Group(); S.espelho.scale.set(1,1,-1);
    S.scene.add(S.mosca); S.scene.add(S.espelho);
    S.macho=new THREE.Group(); S.espelhoM=new THREE.Group(); S.espelhoM.scale.set(1,1,-1); S.scene.add(S.macho); S.scene.add(S.espelhoM);   // o macho: mesma malha, corpo proprio
    S.corpos=j.corpos; S.juntas=j.corpos.map(()=>[]); j.juntas.forEach(jt=>S.juntas[jt.corpo].push(jt));
    S.matriz=j.corpos.map(()=>new THREE.Matrix4());
    S.objs=j.corpos.map(()=>{ const o=new THREE.Object3D(); o.matrixAutoUpdate=false; S.mosca.add(o); return o; });
    S.objsE=j.corpos.map(()=>{ const o=new THREE.Object3D(); o.matrixAutoUpdate=false; S.espelho.add(o); return o; });
    S.matrizM=j.corpos.map(()=>new THREE.Matrix4());
    S.objsM=j.corpos.map(()=>{ const o=new THREE.Object3D(); o.matrixAutoUpdate=false; S.macho.add(o); return o; });
    S.objsME=j.corpos.map(()=>{ const o=new THREE.Object3D(); o.matrixAutoUpdate=false; S.espelhoM.add(o); return o; });
    j.juntas.forEach(jt=>{ if(jt.tipo===0) S.raiz=jt.qadr; if(jt.nome) S.asas[jt.nome]=jt.qadr; });
    for(const g of j.geoms){
      const mk=new THREE.Mesh(geos[g.malha], mats[g.mat]); mk.matrixAutoUpdate=false; TR(g.pos,g.quat,mk.matrix); mk.renderOrder=2; S.objs[g.corpo].add(mk);
      const me=new THREE.Mesh(geos[g.malha], matsE[g.mat]); me.matrixAutoUpdate=false; me.matrix.copy(mk.matrix); me.renderOrder=0; S.objsE[g.corpo].add(me);
      const m2=new THREE.Mesh(geos[g.malha], mats[g.mat]); m2.matrixAutoUpdate=false; m2.matrix.copy(mk.matrix); m2.renderOrder=2; S.objsM[g.corpo].add(m2);
      const m2e=new THREE.Mesh(geos[g.malha], matsE[g.mat]); m2e.matrixAutoUpdate=false; m2e.matrix.copy(mk.matrix); m2e.renderOrder=0; S.objsME[g.corpo].add(m2e);
    }
    // chao preto de verdade (sem luz, senao vira uma laje cinza), um pouco translucido para o reflexo aparecer por baixo
    const chao=new THREE.Mesh(new THREE.PlaneGeometry(600,600), new THREE.MeshBasicMaterial({color:0x000000, transparent:true, opacity:0.80, depthWrite:false}));
    chao.renderOrder=1; S.scene.add(chao);
    S.qi=new Float32Array(S.nq); S.qM=new Float32Array(S.nq);
    redimensionar(); new ResizeObserver(redimensionar).observe(S.box);
  }

  function redimensionar(){
    if(!S.ren) return;
    const r=S.box.getBoundingClientRect(); const w=Math.max(2,r.width), h=Math.max(2,r.height);
    S.ren.setPixelRatio(Math.min(1.5, window.devicePixelRatio||1)); S.ren.setSize(w,h,false);
    S.cam.aspect=w/h; S.cam.updateProjectionMatrix();
  }

  // cinematica direta: corpo = pai * T(pos)R(quat) * juntas(q); a junta livre da raiz vem inteira do qpos
  function aplicar(q){ aplicarEm(q, S.objs, S.objsE, S.matriz, null); }
  function aplicarEm(q, objs, objsE, matriz, raizM){
    for(let b=0;b<S.corpos.length;b++){
      const c=S.corpos[b], jl=S.juntas[b], W=matriz[b];
      if(jl.length && jl[0].tipo===0){ const a=jl[0].qadr; if(raizM) W.copy(raizM); else W.compose(V.set(q[a],q[a+1],q[a+2]), Q.set(q[a+4],q[a+5],q[a+6],q[a+3]).normalize(), UM); }
      else{
        TR(c.pos,c.quat,W);
        for(const jt of jl){ const a=jt.qadr;
          if(jt.tipo===3){ M.makeTranslation(jt.pos[0],jt.pos[1],jt.pos[2]); M2.makeRotationAxis(V.set(jt.eixo[0],jt.eixo[1],jt.eixo[2]), q[a]); M3.makeTranslation(-jt.pos[0],-jt.pos[1],-jt.pos[2]); W.multiply(M).multiply(M2).multiply(M3); }
          else if(jt.tipo===2){ M.makeTranslation(jt.eixo[0]*q[a],jt.eixo[1]*q[a],jt.eixo[2]*q[a]); W.multiply(M); }
          else if(jt.tipo===1){ M.makeTranslation(jt.pos[0],jt.pos[1],jt.pos[2]); M2.makeRotationFromQuaternion(Q.set(q[a+1],q[a+2],q[a+3],q[a]).normalize()); M3.makeTranslation(-jt.pos[0],-jt.pos[1],-jt.pos[2]); W.multiply(M).multiply(M2).multiply(M3); }
        }
        if(c.pai>=0) W.premultiply(matriz[c.pai]);
      }
      objs[b].matrix.copy(W); objsE[b].matrix.copy(W);
    }
  }

  function quadro(j, bytes){
    const q=new Float32Array(bytes);
    if(!S.pronto || q.length!==S.nq) return;
    S.q[0]=S.q[1]; S.t[0]=S.t[1]; S.cams[0]=S.cams[1];
    S.q[1]=q; S.t[1]=performance.now(); S.cams[1]=j.cam; S.ultimo=S.t[1];
  }

  function loop(){
    requestAnimationFrame(loop);
    if(!S.pronto || !S.q[1] || !S.cams[1]) return;
    if(performance.now()-S.ultimo>8000) return;                // corpo parado: nao gasta a placa
    let q=S.q[1], c=S.cams[1];
    if(S.q[0] && S.cams[0]){
      const dt=Math.max(1, S.t[1]-S.t[0]); const alpha=Math.max(0, Math.min(1, (performance.now()-ATRASO_MS-S.t[0])/dt));
      const q0=S.q[0], q1=S.q[1], qi=S.qi;
      for(let i=0;i<qi.length;i++) qi[i]=q0[i]+(q1[i]-q0[i])*alpha;
      q=qi;
      const c0=S.cams[0], c1=S.cams[1]; let daz=c1[3]-c0[3]; daz=((daz+Math.PI)%(2*Math.PI)+2*Math.PI)%(2*Math.PI)-Math.PI;
      c=[c0[0]+(c1[0]-c0[0])*alpha, c0[1]+(c1[1]-c0[1])*alpha, c0[2]+(c1[2]-c0[2])*alpha, c0[3]+daz*alpha, c1[4], c1[5]];
    }
    // ela: asas um pouco abertas quando aceita (femea receptiva abre as asas)
    const X=S.sexo; const tt=(performance.now()-X.t0)/1000; const qEla=(X.estado==='mating')?abrirAsas(q, 0.25+0.1*Math.sin(tt*2*Math.PI*X.ritmo)):q;
    aplicar(qEla);
    desenharMacho(qEla, tt);
    const az=c[3], el=c[4], d=c[5];
    S.cam.position.set(c[0]+d*Math.cos(el)*Math.cos(az), c[1]+d*Math.cos(el)*Math.sin(az), c[2]+d*Math.sin(el));
    S.cam.lookAt(c[0],c[1],c[2]);
    S.ren.render(S.scene, S.cam);
  }

  const RM=new THREE.Matrix4(), RO=new THREE.Matrix4(), RQ=new THREE.Quaternion(), RE=new THREE.Euler();
  function abrirAsas(q, ab){ const o=S.qM; o.set(q); if(S.asas.joint_LWing_abre!=null){ o[S.asas.joint_LWing_abre]+=ab; o[S.asas.joint_RWing_abre]+=ab; } return o; }
  const POSES={ // deslocamento do macho em relacao a ela [x para tras, y para o lado, z para cima], guinada, arfagem
    idle:     {p:[-2.6, 1.3, 0.0],  yaw: 0.45, pitch: 0.0},
    courting: {p:[-2.1, 0.9, 0.0],  yaw: 0.25, pitch: 0.0},
    mating:   {p:[-0.62, 0.0, 0.98], yaw: 0.0, pitch:-0.32},
    rejected: {p:[-3.4,-1.4, 0.0],  yaw:-0.6, pitch: 0.0},
  };
  function desenharMacho(q, tt){
    if(S.raiz<0) return;
    const X=S.sexo; const est=X.estado; const alvo=POSES[est]||POSES.idle; const u=Math.min(1,(performance.now()-X.t_est)/700);
    if(!X.pose) X.pose={p:alvo.p.slice(), yaw:alvo.yaw, pitch:alvo.pitch};
    const k=1-Math.pow(0.001, 1/60); for(let i=0;i<3;i++) X.pose.p[i]+=(alvo.p[i]-X.pose.p[i])*k*1.4; X.pose.yaw+=(alvo.yaw-X.pose.yaw)*k*1.4; X.pose.pitch+=(alvo.pitch-X.pose.pitch)*k*1.4;
    const lib=X.libido, ritmo=X.ritmo*(1+0.25*Math.min(1,(X.dnEle.forward||0)/120));   // o cerebro dele acelera o ritmo
    let px=X.pose.p[0], py=X.pose.p[1], pz=X.pose.p[2], yaw=X.pose.yaw, pitch=X.pose.pitch, roll=0;
    const qM=S.qM; qM.set(q);
    if(est==='mating'){ const f=Math.sin(tt*2*Math.PI*ritmo); px+=0.16*(0.35+0.65*lib)*f; pz+=0.05*Math.abs(f); pitch+=0.08*f; roll=0.03*Math.sin(tt*2*Math.PI*ritmo*0.5);
      const ab=0.22+0.12*lib+0.06*Math.sin(tt*2*Math.PI*ritmo*2); if(S.asas.joint_LWing_abre!=null){ qM[S.asas.joint_LWing_abre]+=ab; qM[S.asas.joint_RWing_abre]+=ab; qM[S.asas.joint_LWing_bate]+=0.05*Math.sin(tt*2*Math.PI*ritmo*4); qM[S.asas.joint_RWing_bate]+=0.05*Math.sin(tt*2*Math.PI*ritmo*4); }
      if(S.asas.joint_Head!=null) qM[S.asas.joint_Head]+=0.15+0.1*Math.max(0,f);
      if(S.asas.joint_Proboscis!=null) qM[S.asas.joint_Proboscis]+=0.5*Math.max(0,Math.sin(tt*2*Math.PI*ritmo*0.5));   // lambe a nuca dela
      for(const perna of ['LF','LM','LH','RF','RM','RH']){ const f=S.asas['joint_'+perna+'Femur'], ti=S.asas['joint_'+perna+'Tibia']; if(f!=null) qM[f]-=0.35; if(ti!=null) qM[ti]+=0.45; }   // pernas agarradas nela
    } else if(est==='courting'){ const vib=Math.sin(tt*2*Math.PI*24)*0.22; if(S.asas.joint_LWing_abre!=null){ qM[S.asas.joint_LWing_abre]+=1.15+vib; qM[S.asas.joint_LWing_bate]+=vib*0.5; }   // canta com uma asa
      px+=0.15*Math.sin(tt*2*Math.PI*0.6); py+=0.25*Math.sin(tt*2*Math.PI*0.35); yaw+=0.15*Math.sin(tt*2*Math.PI*0.4);
    } else if(est==='rejected'){ const w=Math.min(1,u*1.6); pz+=1.8*Math.sin(Math.PI*w); roll=2*Math.PI*w*1.5; pitch+=Math.PI*w*0.3;   // chutado: voa para tras dando cambalhota
      if(S.asas.joint_LWing_abre!=null){ qM[S.asas.joint_LWing_abre]+=0.9; qM[S.asas.joint_RWing_abre]+=0.9; }
    } else { py+=0.05*Math.sin(tt*2*Math.PI*0.3); if(S.asas.joint_Head!=null) qM[S.asas.joint_Head]+=0.08*Math.sin(tt*2*Math.PI*0.5); }
    // raiz do macho = raiz dela x deslocamento (no referencial dela: x para a frente, z para cima)
    const a=S.raiz; RM.compose(V.set(q[a],q[a+1],q[a+2]), Q.set(q[a+4],q[a+5],q[a+6],q[a+3]).normalize(), UM);
    RO.compose(V.set(px,py,pz), RQ.setFromEuler(RE.set(roll,pitch,yaw,'ZYX')), V2.set(0.9,0.9,0.9));
    RM.multiply(RO);
    if(est!=='mating' && est!=='rejected'){ const e=RM.elements; e[14]=Math.max(e[14], q[a+2]*0.98); }   // no chao: nao afunda quando ela esta inclinada
    aplicarEm(qM, S.objsM, S.objsME, S.matrizM, RM);
  }
  function sexo(ev){ const X=S.sexo; if(ev.estado && ev.estado!==X.estado){ X.estado=ev.estado; X.t_est=performance.now(); } if(typeof ev.libido==='number') X.libido=ev.libido; if(ev.ritmo_hz) X.ritmo=ev.ritmo_hz; }
  function dnEle(dn){ S.sexo.dnEle=dn||{}; }
  return {init, quadro, sexo, dnEle, tick:loop, estado:S};
})();
