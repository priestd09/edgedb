import copy
from semantix import ast
from semantix.caos import caosql
from semantix.caos.backends import pgsql
from semantix.utils.debug import debug

class Query(object):
    def __init__(self, text, vars=None, context=None):
        self.text = text
        self.vars = vars
        self.context = context


class Alias(str):
    def __new__(cls, value=''):
        return super(Alias, cls).__new__(cls, pgsql.common.caos_name_to_pg_colname(value))

    def __add__(self, other):
        return Alias(super().__add__(other))

    def __radd__(self, other):
        return Alias(str(other) + str(self))

    __iadd__ = __add__


class ParseContextLevel(object):
    def __init__(self, prevlevel=None):
        if prevlevel is not None:
            self.vars = copy.deepcopy(prevlevel.vars)
            self.ctes = copy.deepcopy(prevlevel.ctes)
            self.aliascnt = copy.deepcopy(prevlevel.aliascnt)
            self.ctemap = copy.deepcopy(prevlevel.ctemap)
            self.concept_node_map = copy.deepcopy(prevlevel.concept_node_map)
            self.location = 'query'
        else:
            self.vars = {}
            self.ctes = {}
            self.aliascnt = {}
            self.ctemap = {}
            self.concept_node_map = {}
            self.location = 'query'

    def genalias(self, alias=None, hint=None):
        if alias is None:
            if hint is None:
                hint = 'a'

            if hint not in self.aliascnt:
                self.aliascnt[hint] = 1
            else:
                self.aliascnt[hint] += 1

            alias = hint + str(self.aliascnt[hint])
        elif alias in self.vars:
            raise caosql.CaosQLError('Path var redefinition: % is already used' %  alias)

        return Alias(alias)

class ParseContext(object):
    stack = []

    def __init__(self):
        self.push()

    def push(self):
        level = ParseContextLevel()
        self.stack.append(level)

        return level

    def pop(self):
        self.stack.pop()

    def _current(self):
        if len(self.stack) > 0:
            return self.stack[-1]
        else:
            return None

    current = property(_current)


class CaosQLQueryAdapter(ast.visitor.NodeVisitor):
    @debug
    def adapt(self, query, vars=None):
        # Transform to sql tree
        qtree = self._transform_tree(query)

        """LOG [caos.query] SQL Tree
        self._dump(qtree)
        """

        # Generate query text
        qtext = pgsql.codegen.SQLSourceGenerator.to_source(qtree)

        """LOG [caos.query] SQL Query
        from semantix.utils.debug import highlight
        print(highlight(qtext, 'sql'))
        """

        return Query(qtext, vars)

    def _dump(self, tree):
        print(tree.dump(pretty=True, colorize=True, width=180, field_mask='^(_.*)$'))

    def _transform_tree(self, tree):

        context = ParseContext()
        context.current.query = pgsql.ast.SelectQueryNode()

        self._process_paths(context, tree.paths)
        self._process_generator(context, tree.generator)
        self._process_selector(context, tree.selector)
        self._process_sorter(context, tree.sorter)

        return context.current.query

    def _process_generator(self, context, generator):
        query = context.current.query
        query.where = self._process_expr(context, generator)

    def _process_selector(self, context, selector):
        query = context.current.query

        context.current.location = 'selector'
        for expr in selector:
            target = pgsql.ast.SelectExprNode(expr=self._process_expr(context, expr.expr), alias=expr.name)
            query.targets.append(target)

    def _process_sorter(self, context, sorter):
        query = context.current.query
        context.current.location = 'sorter'

        for expr in sorter:
            sortexpr = pgsql.ast.SortExprNode(expr=self._process_expr(context, expr.expr),
                                           direction=expr.direction)
            query.orderby.append(sortexpr)

    def _process_paths(self, context, paths):
        query = context.current.query

        for path in paths:
            expr = self._process_expr(context, path)
            if expr:
                query.fromlist.append(expr)

    def _process_expr(self, context, expr):
        result = None

        expr_t = type(expr)

        if expr_t == caosql.ast.ExistPred:
            result = self._process_expr(context, expr.expr)

        elif expr_t == caosql.ast.EntitySet:
            self._process_graph(context, context.current.query, expr)

        elif expr_t == caosql.ast.BinOp:
            left = self._process_expr(context, expr.left)
            right = self._process_expr(context, expr.right)
            result = pgsql.ast.BinOpNode(op=expr.op, left=left, right=right)

        elif expr_t == caosql.ast.Constant:
            result = pgsql.ast.ConstantNode(value=expr.value)

        elif expr_t == caosql.ast.Sequence:
            elements = [self._process_expr(context, e) for e in expr.elements]
            result = pgsql.ast.SequenceNode(elements=elements)

        elif expr_t == caosql.ast.FunctionCall:
            args = [self._process_expr(context, a) for a in expr.args]
            result = pgsql.ast.FunctionCallNode(name=expr.name, args=args)

        elif expr_t == caosql.ast.AtomicRef:
            if isinstance(expr.ref(), caosql.ast.EntitySet):
                fieldref = context.current.concept_node_map[expr.ref()]['id']
            else:
                fieldref = expr.ref()

            datatable = None

            if isinstance(fieldref, pgsql.ast.FieldRefNode):
                if fieldref.field == expr.name:
                    return fieldref
                else:
                    datatable = fieldref.table

            if isinstance(fieldref, pgsql.ast.SelectExprNode):
                fieldref_expr = fieldref.expr
            else:
                fieldref_expr = fieldref

            if expr.expr is not None:
                return self._process_expr(context, expr.expr)

            if context.current.location in ('selector', 'sorter'):
                if expr.name == 'id':
                    result = fieldref_expr
                else:
                    if not datatable:
                        query = context.current.query

                        datatable = self._relation_from_concepts(context, expr.ref().concepts, expr.ref().id)
                        #datatable = context.current.ctemap[expr.ref()]

                        query.fromlist.append(datatable)

                        left = fieldref_expr
                        right = pgsql.ast.FieldRefNode(table=datatable, field='id')
                        whereexpr = pgsql.ast.BinOpNode(op='=', left=left, right=right)
                        if query.where is not None:
                            query.where = pgsql.ast.BinOpNode(op='and', left=query.where, right=whereexpr)
                        else:
                            query.where = whereexpr

                    result = pgsql.ast.FieldRefNode(table=datatable, field=expr.name)
                    context.current.concept_node_map[expr.ref()]['id'] = result
            else:
                if isinstance(expr.ref(), caosql.ast.EntitySet):
                    result = fieldref_expr
                else:
                    result = pgsql.ast.FieldRefNode(table=fieldref, field=expr.name)

        elif expr_t == caosql.ast.MetaRef:
            if context.current.location not in ('selector', 'sorter'):
                raise caosql.CaosQLError('meta references are currently only supported in selectors and sorters')

            fieldref = context.current.concept_node_map[expr.ref()]['class_id']
            query = context.current.query
            datatable = pgsql.ast.TableNode(name='metaobject',
                                            schema='caos',
                                            concepts=None,
                                            alias=context.current.genalias(hint='metaobject'))
            query.fromlist.append(datatable)

            left = fieldref.expr
            right = pgsql.ast.FieldRefNode(table=datatable, field='id')
            whereexpr = pgsql.ast.BinOpNode(op='=', left=left, right=right)
            if query.where is not None:
                query.where = pgsql.ast.BinOpNode(op='and', left=query.where, right=whereexpr)
            else:
                query.where = whereexpr

            result = pgsql.ast.FieldRefNode(table=datatable, field=expr.name)

        return result

    def _attr_in_table(self, context, table, attr):
        if isinstance(table, pgsql.ast.TableNode):
            """
            It's either "entity_map" or one of the "*_data" tables.
            Entity data table is guaranteed to have the attr here, but entity map
            obviously can only yield ids.
            """
            return table.name == 'entity_map'
        elif isinstance(table, pgsql.ast.SelectQueryNode):
            """
            For Select we check the aliases
            """
            for target in table.targets:
                if target.alias == attr:
                    return True

        return False

    def _process_graph(self, context, cte, startnode):
        # Avoid processing the same subgraph more than once
        if startnode in context.current.ctemap:
            return

        fromnode = pgsql.ast.FromExprNode()
        cte.fromlist.append(fromnode)

        fromnode.expr = self._process_path(context, cte, None, startnode)

    def _simple_join(self, context, left, right, key, type='inner'):
        condition = left.bonds(key)[-1]
        if not isinstance(condition, pgsql.ast.BinOpNode):
            condition = pgsql.ast.BinOpNode(op='=', left=left.bonds(key)[-1], right=right.bonds(key)[-1])
        join = pgsql.ast.JoinNode(type=type, left=left, right=right, condition=condition)

        join.updatebonds(left)
        join.updatebonds(right)

        return join

    def _relation_from_concepts(self, context, concepts, alias_hint=None):
        if len(concepts) == 1:
            concept = next(iter(concepts))
            table_name, table_schema_name = self._caos_name_to_pg_table(concept.name)
            concept_table = pgsql.ast.TableNode(name=table_name,
                                                schema=table_schema_name,
                                                concepts=concepts,
                                                alias=context.current.genalias(hint=table_name))
        elif len(concepts) == 0:
            concept_table = pgsql.ast.TableNode(name='entity',
                                                schema='caos',
                                                concepts=concepts,
                                                alias=context.current.genalias(hint='entity'))
        else:
            ##
            # If several concepts are specified in the node, it is a so-called parallel path
            # and is translated into a UNION ALL of SELECTs from each of the specified
            # concept tables.  Assuming that database planner does the right thing this
            # should be functionally equivalent to using a higher table with constraint
            # exclusions.
            #
            concept_table = pgsql.ast.UnionNode(concepts=concepts,
                                                alias=context.current.genalias(hint=alias_hint))

            for concept in concepts:
                table_name, table_schema_name = self._caos_name_to_pg_table(concept.name)
                table = pgsql.ast.TableNode(name=table_name,
                                            schema=table_schema_name,
                                            concepts={concept},
                                            alias=context.current.genalias(hint=table_name))

                fromexpr = pgsql.ast.FromExprNode(expr=table)
                targets = []
                fieldref = pgsql.ast.FieldRefNode(table=table, field='id')
                targets.append(pgsql.ast.SelectExprNode(expr=fieldref))
                fieldref = pgsql.ast.FieldRefNode(table=table, field='concept_id')
                targets.append(pgsql.ast.SelectExprNode(expr=fieldref))
                query = pgsql.ast.SelectQueryNode(targets=targets, fromlist=[fromexpr])

                concept_table.queries.append(query)

        return concept_table

    def _get_step_cte(self, context, cte, step, joinpoint, link):
        """
        Generates a Common Table Expression for a given step in the path

        @param context: parse context
        @param cte: parent CTE
        @param step: CaosQL path step expression
        @param joinpoint: current position in parent CTE join chain
        """

        # Avoid processing the same step twice
        if step in context.current.ctemap:
            return context.current.ctemap[step]

        if step.name:
            cte_alias = context.current.genalias(alias=step.name)
        else:
            cte_alias = context.current.genalias(hint=step.id)

        step_cte = pgsql.ast.SelectQueryNode(concepts=step.concepts, alias=cte_alias)
        context.current.ctemap[step] = step_cte

        fromnode = pgsql.ast.FromExprNode()

        concept_table = self._relation_from_concepts(context, step.concepts, alias_hint=step.id)

        field_name = 'id'
        bond = pgsql.ast.FieldRefNode(table=concept_table, field=field_name)
        concept_table.addbond(step.concepts, bond)

        if joinpoint is None:
            fromnode.expr = concept_table
        else:
            target_id_field = pgsql.ast.FieldRefNode(table=concept_table, field='id')

            #
            # Append the step to the join chain taking link filter into account
            #

            if link.filter:
                if link.filter.direction == link.filter.BACKWARD:
                    source_fld = 'target_id'
                    target_fld = 'source_id'
                else:
                    source_fld = 'source_id'
                    target_fld = 'target_id'

                join = joinpoint

                target_bond_expr = None

            if link.filter and link.filter.labels:
                #
                # If specific links are provided we LEFT JOIN all corresponding link tables and then
                # filter out all empty rows in a single join condition
                #
                for label in link.filter.labels:
                    map = pgsql.ast.TableNode(name=label.name.name + '_link',
                                              schema='caos_' + label.name.module, concepts=step.concepts,
                                              alias=context.current.genalias(hint='map'))
                    map.addbond(link.source.concepts, pgsql.ast.FieldRefNode(table=map, field=source_fld))
                    join = self._simple_join(context, joinpoint, map, link.source.concepts, type='left')

                    cond_expr = pgsql.ast.BinOpNode(left=pgsql.ast.FieldRefNode(table=map, field=target_fld),
                                                    op='=', right=target_id_field)

                    if target_bond_expr:
                        target_bond_expr = pgsql.ast.BinOpNode(left=target_bond_expr, op='or',
                                                               right=cond_expr)
                    else:
                        target_bond_expr = cond_expr

            else:
                #
                # Generic link existence check is the simplest case: just INNER JOIN the main
                # map table.
                #
                map = pgsql.ast.TableNode(name='entity_map', schema='caos', concepts=step.concepts,
                                          alias=context.current.genalias(hint='map'))

                map.addbond(link.source.concepts, pgsql.ast.FieldRefNode(table=map, field=source_fld))
                join = self._simple_join(context, joinpoint, map, link.source.concepts)

                target_bond_expr = pgsql.ast.FieldRefNode(table=map, field=target_fld)

            join.addbond(step.concepts, target_bond_expr)
            join = self._simple_join(context, join, concept_table, step.concepts)

            fromnode.expr = join

            #
            # Pull the references to fields inside the CTE one level up to keep
            # them visible.
            #
            for concept_node, refs in context.current.concept_node_map.items():
                for refrole, ref in refs.items():
                    if ref.alias in joinpoint.concept_node_map:
                        refexpr = pgsql.ast.FieldRefNode(table=joinpoint, field=ref.alias)
                        fieldref = pgsql.ast.SelectExprNode(expr=refexpr, alias=ref.alias)
                        step_cte.targets.append(fieldref)
                        step_cte.concept_node_map[ref.alias] = fieldref

                        bondref = pgsql.ast.FieldRefNode(table=step_cte, field=ref.alias)
                        step_cte.addbond(concept_node.concepts, bondref)
                        context.current.concept_node_map[concept_node][refrole].expr.table = step_cte

        # Include target entity id and metaclass id in the Select expression list ...
        fieldref = fromnode.expr.bonds(step.concepts)[-1]
        selectnode = pgsql.ast.SelectExprNode(expr=fieldref, alias=step_cte.alias + '_entity_id', role='id')
        step_cte.targets.append(selectnode)

        fieldref = pgsql.ast.FieldRefNode(table=fieldref.table, field='concept_id')
        selectnode_class = pgsql.ast.SelectExprNode(expr=fieldref, alias=step_cte.alias + '_concept_id',
                                                    role='class_id')
        step_cte.targets.append(selectnode_class)

        step_cte.concept_node_map[selectnode.alias] = selectnode
        step_cte.concept_node_map[selectnode_class.alias] = selectnode_class

        # ... and record them in global map in case they have to be pulled up later
        refexpr = pgsql.ast.FieldRefNode(table=step_cte, field=selectnode.alias)
        selectnode = pgsql.ast.SelectExprNode(expr=refexpr, alias=selectnode.alias, role='id')
        refexpr = pgsql.ast.FieldRefNode(table=step_cte, field=selectnode_class.alias)
        selectnode_class = pgsql.ast.SelectExprNode(expr=refexpr, alias=selectnode_class.alias,
                                                    role='class_id')
        context.current.concept_node_map[step] = {selectnode.role: selectnode,
                                                  selectnode_class.role: selectnode_class}

        if step.filter:
            def filter_atomic_refs(node):
                return isinstance(node, caosql.ast.AtomicRef) # and node.refs[0] == step

            # Fixup atomic refs sources
            atrefs = self.find_children(step.filter, filter_atomic_refs)

            if atrefs:
                for atref in atrefs:
                    atref.refs.pop()
                    atref.refs.add(concept_table)

            expr = pgsql.ast.PredicateNode(expr=self._process_expr(context, step.filter))
            if step_cte.where is not None:
                step_cte.where = pgsql.ast.BinOpNode(op='and', left=step_cte.where, right=expr)
            else:
                step_cte.where = expr

        step_cte.fromlist.append(fromnode)
        step_cte._source_graph = step

        bond = pgsql.ast.FieldRefNode(table=step_cte, field=step_cte.alias + '_entity_id')
        step_cte.addbond(step.concepts, bond)

        return step_cte

    def _caos_name_to_pg_table(self, name):
        # XXX: TODO: centralize this with pgsql backend
        return name.name + '_data', 'caos_' + name.module

    def _process_path(self, context, cte, joinpoint, pathtip):
        join = joinpoint

        if join is None:
            join = self._get_step_cte(context, cte, pathtip, None, None)

        for link in pathtip.links:
            join = self._get_step_cte(context, cte, link.target, join, link)
            join = self._process_path(context, cte, join, link.target)

        return join
