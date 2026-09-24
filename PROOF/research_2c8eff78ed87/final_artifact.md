# Final research artifact: local bifurcation at the first Dirichlet eigenvalue

Let \(\Omega\subset\mathbb R^n\) be bounded and connected with \(C^{2,\alpha}\) boundary, \(0<\alpha<1\). Let \(\lambda_1\) be the first Dirichlet eigenvalue of \(-\Delta\), and let \(\phi_1>0\) be a corresponding eigenfunction. Put
\[
N=\int_\Omega\phi_1^2\,dx>0,\qquad A_4=\int_\Omega\phi_1^4\,dx>0.
\]
The normalization of \(\phi_1\) is arbitrary; if it is \(L^2\)-normalized, \(N=1\).

## Theorem

For the Dirichlet problem
\[
-\Delta u=\lambda u+u^3\quad\text{in }\Omega,\qquad u|_{\partial\Omega}=0,
\]
there are \(\varepsilon>0\) and smooth maps \(s\mapsto\lambda(s)\) and \(s\mapsto u(s)\in X\), where
\[
X=\{u\in C^{2,\alpha}(\overline\Omega):u|_{\partial\Omega}=0\},
\]
such that \((\lambda(0),u(0))=(\lambda_1,0)\), \(u(s)\ne0\) for \(0<|s|<\varepsilon\), and
\[
u(s)=s\phi_1+O(s^3)\quad\text{in }X,\qquad
\lambda(s)=\lambda_1-\frac{A_4}{N}s^2+O(s^4).
\]
In particular \((\lambda_1,0)\) is a local bifurcation point and \(\lambda(s)<\lambda_1\) for sufficiently small nonzero \(s\). Moreover,
\(\lambda(-s)=\lambda(s)\), \(u(-s)=-u(s)\), and, in a sufficiently small neighborhood of \((\lambda_1,0)\) in \(\mathbb R\times X\), every nontrivial solution belongs to this curve. The two nontrivial arms form a left-bending pitchfork.

## Proof

### 1. Linear operator and its range

Let \(Y=C^{0,\alpha}(\overline\Omega)\) and
\[
F(\lambda,u)=-\Delta u-\lambda u-u^3:\mathbb R\times X\longrightarrow Y.
\]
This polynomial map is \(C^\infty\), and \(F(\lambda,0)=0\). The Dirichlet operator \(A=-\Delta:X\to Y\) is an isomorphism by Schauder theory. The inclusion \(J:X\hookrightarrow Y\) is compact, so
\(L=D_uF(\lambda_1,0)=A-\lambda_1J\) is Fredholm of index zero.

The standard principal Dirichlet eigenvalue theorem on a connected domain gives
\(\ker L=\operatorname{span}\{\phi_1\}\). Integration by parts shows
\(\int_\Omega\phi_1 Lv\,dx=0\) for every \(v\in X\). Since \(L\) has codimension-one range,
\[
\operatorname{Range}L
=\left\{f\in Y:\int_\Omega\phi_1 f\,dx=0\right\}.
\]
Also \(D_{\lambda u}^2F(\lambda_1,0)[\phi_1]=-\phi_1\) lies outside this range because its pairing with \(\phi_1\) equals \(-N\). Thus the usual simple-eigenvalue transversality condition holds.

### 2. Auxiliary equation and symmetry

Set
\[
W=\left\{v\in X:\int_\Omega\phi_1v\,dx=0\right\},\qquad
Qf=f-\frac{\int_\Omega\phi_1f\,dx}{N}\phi_1.
\]
Then \(X=\operatorname{span}\{\phi_1\}\oplus W\), \(QY=\operatorname{Range}L\), and
\(L|_W:W\to QY\) is an isomorphism. Every \(u\in X\) has a unique expression \(u=s\phi_1+w\), \(w\in W\).

Apply the Banach-space implicit function theorem to
\[
G(s,\lambda,w)=QF(\lambda,s\phi_1+w)=0
\]
at \((0,\lambda_1,0)\). It gives a unique smooth \(w=w(s,\lambda)\in W\) near that point. Since \(G(0,\lambda,0)=0\), uniqueness gives \(w(0,\lambda)=0\) for every nearby \(\lambda\). Further,
\[
G_s(0,\lambda,0)=Q[(\lambda_1-\lambda)\phi_1]=0.
\]
The operator \(G_w(0,\lambda,0)\) remains invertible for nearby \(\lambda\); differentiating \(G(s,\lambda,w(s,\lambda))=0\) therefore gives \(w_s(0,\lambda)=0\).

Because \(F(\lambda,-u)=-F(\lambda,u)\), uniqueness of the auxiliary solution gives
\[
w(-s,\lambda)=-w(s,\lambda).
\]
Thus \(w\) is odd in \(s\). Its zeroth and first derivatives at \(s=0\) vanish, and oddness also makes its second derivative vanish. Taylor's theorem, uniformly for nearby \(\lambda\), yields
\[
\|w(s,\lambda)\|_X\le C|s|^3. \tag{1}
\]

### 3. Scalar equation and the coefficient

Define the unnormalized scalar equation
\[
g(s,\lambda)=\int_\Omega\phi_1 F(\lambda,s\phi_1+w(s,\lambda))\,dx.
\]
Integration by parts and \(w\in W\) give
\(\int_\Omega\phi_1(-\Delta-\lambda)w\,dx=0\). Expanding the cubic term and using (1), uniformly near \(\lambda_1\),
\[
g(s,\lambda)
=(\lambda_1-\lambda)Ns-A_4s^3+O(s^5). \tag{2}
\]
The \(O(s^5)\) remainder follows in particular from
\(s^2w=O(s^5)\); it would not follow from \(w=O(s^2)\) alone.

The function \(g\) is smooth and odd in \(s\), so the divided function
\[
H(s,\lambda)=\int_0^1\partial_sg(ts,\lambda)\,dt
\]
is smooth and even in \(s\), and \(g(s,\lambda)=sH(s,\lambda)\). From (2),
\[
H(s,\lambda)=(\lambda_1-\lambda)N-A_4s^2+O(s^4),\qquad
\partial_\lambda H(0,\lambda_1)=-N\ne0.
\]
The scalar implicit function theorem supplies a unique smooth \(\lambda=\lambda(s)\) near zero with \(H(s,\lambda(s))=0\) and \(\lambda(0)=\lambda_1\). Evenness of \(H\) and uniqueness imply \(\lambda(-s)=\lambda(s)\). Substitution into the expansion gives
\[
\lambda(s)=\lambda_1-\frac{A_4}{N}s^2+O(s^4).
\]
Since \(A_4/N>0\), this is less than \(\lambda_1\) when \(s\ne0\) is sufficiently small. Set \(u(s)=s\phi_1+w(s,\lambda(s))\). Equation (1) gives \(u(s)=s\phi_1+O(s^3)\), so \(u(s)\ne0\) for sufficiently small \(s\ne0\). Oddness of \(w\) and evenness of \(\lambda\) give \(u(-s)=-u(s)\).

### 4. Exhaustion of nearby solutions

Take any solution \((\lambda,u)\) close enough to \((\lambda_1,0)\) in \(\mathbb R\times X\). Decompose it uniquely as \(u=s\phi_1+w\), \(w\in W\). The auxiliary equation and its local uniqueness force \(w=w(s,\lambda)\). If \(s=0\), then \(w=w(0,\lambda)=0\), so the solution is trivial. If \(s\ne0\), the remaining scalar equation \(g(s,\lambda)=0\) is equivalent to \(H(s,\lambda)=0\), whose local uniqueness forces \(\lambda=\lambda(s)\). Thus every nearby nontrivial solution is exactly \((\lambda(s),u(s))\). This proves local uniqueness and the stated pitchfork symmetry. \(\square\)

## References

- M. G. Crandall and P. H. Rabinowitz, *Bifurcation from Simple Eigenvalues*, Journal of Functional Analysis 8 (1971), 321–340. DOI: 10.1016/0022-1236(71)90015-2.
- Classical Schauder and principal Dirichlet eigenvalue theorems are used for the elliptic setup. The branch and its coefficient are derived above by Lyapunov–Schmidt reduction.

## Artifact files

- Complete proof: PROOF/research_2c8eff78ed87/method_01/proof.md
- LaTeX version: PROOF/research_2c8eff78ed87/method_01/bifurcation_proof.tex
