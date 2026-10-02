package com.acme.rules

import org.psi.TreeVisitor

// A rule hierarchy in the shape of detekt's: the base class extends an external visitor, rules
// override `visit`, and a rule set runs its rules from an extension function.
abstract class BaseRule : TreeVisitor() {
    fun visitFile(root: String, extra: Int = 0) {}
    open fun visit(root: String) {}
}

abstract class MultiRule : BaseRule()

class RuleSet(val id: String, val rules: List<BaseRule>)

// `rules` is `this.rules` on the extension receiver: it.visitFile is BaseRule.visitFile
fun RuleSet.visitAll(file: String): Int = rules.sumOf {
    it.visitFile(file)
    1
}

// annotated, fully qualified supertype: must still parse as a class with members, extending MultiRule
class AnnotatedRule :
    @Suppress("DEPRECATION")
    com.acme.rules.MultiRule() {
    override fun visit(root: String) {}
}

// a private overload of `visit` is not an override of BaseRule.visit
class OverloadRule : BaseRule() {
    private fun visit(element: Int) {}
}

// overrides the external visitor's visitFile(PsiFile): `super.visitFile` is the external one
class PsiRule : BaseRule() {
    override fun visitFile(file: PsiFile) {
        super.visitFile(file)
    }
}
