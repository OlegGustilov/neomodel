"""
Copyright (c) Django Software Foundation and individual contributors.
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice,
       this list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright
       notice, this list of conditions and the following disclaimer in the
       documentation and/or other materials provided with the distribution.

    3. Neither the name of Django nor the names of its contributors may be used
       to endorse or promote products derived from this software without
       specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


A class for storing a tree graph. Primarily used for filter constructs in the
ORM.
"""

import copy


class QBase:
    """
    A single internal node in the tree graph. A Node should be viewed as a
    connection (the root) with the children being either leaf nodes or other
    Node instances.
    """

    # Standard connector type. Clients usually won't use this at all and
    # subclasses will usually override the value.
    default = "DEFAULT"

    def __init__(self, children=None, connector=None, negated=False):
        """Construct a new Node. If no connector is given, use the default."""
        self.children = children[:] if children else []
        self.connector = connector or self.default
        self.negated = negated

    # Required because django.db.models.query_utils.Q. Q. __init__() is
    # problematic, but it is a natural Node subclass in all other respects.
    @classmethod
    def _new_instance(cls, children=None, connector=None, negated=False):
        """
        Create a new instance of this class when new Nodes (or subclasses) are
        needed in the internal code in this class. Normally, it just shadows
        __init__(). However, subclasses with an __init__ signature that aren't
        an extension of Node.__init__ might need to implement this method to
        allow a Node to create a new instance of them (if they have any extra
        setting up to do).
        """
        obj = QBase(children, connector, negated)
        obj.__class__ = cls
        return obj

    def __str__(self):
        return f"(NOT ({self.connector}: {', '.join(str(c) for c in self.children)}))" if self.negated else f"({self.connector}: {', '.join(str(c) for c in self.children)})"

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self}>"

    def __deepcopy__(self, memodict):
        obj = QBase(connector=self.connector, negated=self.negated)
        obj.__class__ = self.__class__
        obj.children = copy.deepcopy(self.children, memodict)
        return obj

    def __len__(self):
        """Return the the number of children this node has."""
        return len(self.children)

    def __bool__(self):
        """Return whether or not this node has children."""
        return bool(self.children)

    def __contains__(self, other):
        """Return True if 'other' is a direct child of this instance."""
        return other in self.children

    def __eq__(self, other):
        if self.__class__ != other.__class__:
            return False
        if (self.connector, self.negated) == (other.connector, other.negated):
            return self.children == other.children
        return False

    def __hash__(self):
        return hash(
            (self.__class__, self.connector, self.negated) + tuple(self.children)
        )

    def add(self, data, conn_type, squash=True):
        """
        Combine this tree and the data represented by data using the
        connector conn_type. The combine is done by squashing the node other
        away if possible.

        This tree (self) will never be pushed to a child node of the
        combined tree, nor will the connector or negated properties change.

        Return a node which can be used in place of data regardless if the
        node other got squashed or not.

        If `squash` is False the data is prepared and added as a child to
        this tree without further logic.

        Args:
            conn_type (str, optional ["AND", "OR"]): connection method
        """
        if data in self.children:
            return data
        if not squash:
            self.children.append(data)
            return data
        if self.connector == conn_type:
            # We can reuse self.children to append or squash the node other.
            if (
                isinstance(data, QBase)
                and not data.negated
                and (data.connector == conn_type or len(data) == 1)
            ):
                # We can squash the other node's children directly into this
                # node. We are just doing (AB)(CD) == (ABCD) here, with the
                # addition that if the length of the other node is 1 the
                # connector doesn't matter. However, for the len(self) == 1
                # case we don't want to do the squashing, as it would alter
                # self.connector.
                self.children.extend(data.children)
                return self

            # We could use perhaps additional logic here to see if some
            # children could be used for pushdown here.
            self.children.append(data)
            return data

        obj = self._new_instance(self.children, self.connector, self.negated)
        self.connector = conn_type
        self.children = [obj, data]
        return data

    def negate(self):
        """Negate the sense of the root connector."""
        self.negated = not self.negated

    @classmethod
    def from_django_q(cls, q):
        def _convert(query):
            _children = []
            for child in query.children:
                if hasattr(child, '__getitem__'):
                    _children.append(child)
                else:
                    _children.append(QBase(_convert(child), child.connector, child.negated))
            return _children

        q = Q(q, _connector=q.connector, _negated=q.negated)
        return _convert(q)[0]


class Q(QBase):
    """
    Encapsulate filters as objects that can then be combined logically (using
    `&` and `|`).
    """

    # Connection types
    AND = "AND"
    OR = "OR"
    default = AND

    def __init__(self, *args, **kwargs):
        connector = kwargs.pop("_connector", None)
        negated = kwargs.pop("_negated", False)
        super().__init__(
            children=list(args) + sorted(kwargs.items()),
            connector=connector,
            negated=negated,
        )

    def _combine(self, other, conn):
        if not isinstance(other, Q) and not isinstance(other, QBase):
            raise TypeError(other)

        # If the other Q() is empty, ignore it and just use `self`.
        if not other:
            return copy.deepcopy(self)
        # Or if this Q is empty, ignore it and just use `other`.
        if not self:
            return copy.deepcopy(other)

        obj = type(self)()
        obj.connector = conn
        obj.add(self, conn)
        obj.add(other, conn)
        return obj

    def __or__(self, other):
        return self._combine(other, self.OR)

    def __and__(self, other):
        return self._combine(other, self.AND)

    def __invert__(self):
        obj = type(self)()
        obj.add(self, self.AND)
        obj.negate()
        return obj
